"""Can the retriever even reach the facts the eval set asks for? No LLM.

    python evals/retrieval.py --site sepadental
    python evals/retrieval.py --site sepadental --sweep

Every row in the eval set names the strings a correct reply must contain.
This checks whether those strings are in the context the model would be
given. A row that fails here can never pass evals/run.py, whatever model or
prompt is used — so this is the ceiling on accuracy.

It spends no generation quota, so it runs in seconds and can be used to tune
retrieval as often as you like. It is not literally free: embeddings have
their own free-tier cap (1000/day) and re-running this uncached burned it in
one afternoon. Query vectors are therefore cached on disk, so the second and
later runs cost nothing. Use it before spending an LLM run.

Three outcomes per fact:

    reached      in the top-KEEP context — the model has it
    ranked N     in the index, but below the cutoff  -> retrieval tuning
    ABSENT       not in any chunk on the site        -> crawl/extraction bug

The distinction matters: the second is a knob, the first cost us three
tuning runs aimed at the prompt.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from ingest.embed import EMBED_DIM, EMBED_MODEL, embed_query  # noqa: E402
from ingest.store import connect  # noqa: E402
from query import CANDIDATES, KEEP, build_context, search  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The scorer's own matcher, so this check and evals/run.py agree on what
# counts as present. Comparing raw text here reported every opening hour as
# missing from the index: rows ask for "9.00am" and chunks store "9 AM".
from run import contains  # noqa: E402

HERE = Path(__file__).resolve().parent
DEEP = 60          # how far down the ranking to look before calling it absent

# Embeddings are NOT free: the free tier allows 1000 embed calls per day,
# a separate bucket from generation. Re-embedding the same fixed eval
# questions on every run exhausted it in an afternoon. The questions never
# change, so cache them on disk and a re-run costs nothing at all.
QCACHE = (HERE.parent / "data" / "vectors"
          / f"queries-{EMBED_MODEL}-{EMBED_DIM}.npz")


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_qcache() -> dict[str, np.ndarray]:
    if not QCACHE.exists():
        return {}
    try:
        z = np.load(QCACHE, allow_pickle=False)
        return dict(zip(z["hashes"].tolist(), z["vectors"]))
    except Exception as exc:
        print(f"  query cache unreadable ({exc}) — rebuilding")
        return {}


def save_qcache(cache: dict[str, np.ndarray]) -> None:
    if not cache:
        return
    QCACHE.parent.mkdir(parents=True, exist_ok=True)
    np.savez(QCACHE, hashes=np.array(list(cache)),
             vectors=np.stack(list(cache.values())))


def embed_cached(text: str, cache: dict[str, np.ndarray]) -> list[float]:
    k = _key(text)
    if k not in cache:
        cache[k] = np.asarray(embed_query(text), dtype=np.float32)
    return cache[k].tolist()


def requirements(row: dict) -> list[tuple[str, list[str]]]:
    """(label, alternatives) for each thing the row demands.

    `required` is all-of, so each string is its own requirement.
    `required_any` is one requirement satisfied by any of its strings —
    scoring them separately turns a satisfied row into several misses. The
    zh rows say `required_any: [implant, 植]` precisely so a Chinese reply
    can satisfy them; counting 植 alone made retrieval look broken.
    """
    ev = row.get("evaluation") or {}
    out = [(t, [t]) for t in (ev.get("required") or [])]
    if alts := (ev.get("required_any") or []):
        out.append((" | ".join(alts), alts))
    return out


def in_index(conn, site: str, needle: str) -> bool:
    with conn.cursor() as c:
        c.execute("SELECT content FROM chunks WHERE site=%s", (site,))
        return any(contains(row[0], needle) for row in c.fetchall())


def probe(conn, site: str, rows: list[dict], keep: int,
          hits_by_q: dict[str, list[dict]]) -> list[dict]:
    """One record per requirement.

    `hits_by_q` memoises the ranking per question. The ranking does not
    depend on KEEP — only the slice fed to the model does — so a sweep must
    reuse it instead of re-running the search six times.
    """
    out = []
    for row in rows:
        reqs = requirements(row)
        if not reqs:
            continue
        hits = hits_by_q[row["question"]]
        ctx = build_context(hits[:keep])
        for label, alts in reqs:
            ranks = [r for r in
                     (next((i for i, h in enumerate(hits, 1)
                            if contains(h["content"], a)), None)
                      for a in alts) if r]
            out.append({
                "question": row["question"],
                "lang": row.get("lang", "?"),
                "need": label,
                "reached": any(contains(ctx, a) for a in alts),
                "rank": min(ranks) if ranks else None,
                "indexed": bool(ranks) or any(in_index(conn, site, a) for a in alts),
            })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--keep", type=int, default=KEEP)
    ap.add_argument("--sweep", action="store_true",
                    help="compare several KEEP values instead of listing gaps")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    rows = yaml.safe_load((HERE / f"{args.site}.yaml").read_text(encoding="utf-8"))
    conn = connect()

    # Search once per question, reuse for every KEEP.
    cache = load_qcache()
    before = len(cache)
    hits_by_q: dict[str, list[dict]] = {}
    for row in rows:
        q = row["question"]
        if requirements(row) and q not in hits_by_q:
            hits_by_q[q] = search(conn, args.site, embed_cached(q, cache),
                                  limit=DEEP)
    save_qcache(cache)
    fresh = len(cache) - before
    print(f"  {len(hits_by_q)} questions   "
          f"{len(hits_by_q) - fresh} embeddings from cache, {fresh} computed")

    if args.sweep:
        print(f"\n{args.site}: facts reaching the model, by KEEP "
              f"(CANDIDATES={CANDIDATES})\n")
        for k in (1, 2, 3, 5, 8, 12):
            recs = probe(conn, args.site, rows, k, hits_by_q)
            hit = sum(r["reached"] for r in recs)
            print(f"  KEEP={k:<3} {hit}/{len(recs)}  ({100*hit/len(recs):.0f}%)")
        return 0

    recs = probe(conn, args.site, rows, args.keep, hits_by_q)
    hit = [r for r in recs if r["reached"]]
    absent = [r for r in recs if not r["reached"] and not r["indexed"]]
    low = [r for r in recs if not r["reached"] and r["indexed"]]

    print("\n" + "=" * 72)
    print(f"  {args.site}   KEEP={args.keep}   {len(recs)} required facts "
          f"across {len({r['question'] for r in recs})} rows")
    print("=" * 72)
    print(f"  reached the model   {len(hit)}/{len(recs)}  "
          f"({100*len(hit)/len(recs):.0f}%)   <-- ceiling on accuracy")
    print(f"  ranked too low      {len(low)}/{len(recs)}   (retrieval tuning)")
    print(f"  not in the index    {len(absent)}/{len(recs)}   (crawl/extraction)")

    for label, group in (("NOT IN THE INDEX AT ALL", absent),
                         ("IN THE INDEX BUT RANKED TOO LOW", low)):
        if not group:
            continue
        print(f"\n  {label}")
        for r in sorted(group, key=lambda x: (x["rank"] or 999)):
            where = f"rank {r['rank']}" if r["rank"] else "absent"
            print(f"    {r['lang']:<6} {r['question'][:42]:<44} "
                  f"{r['need'][:24]:<26} {where}")

    by_lang = Counter(r["lang"] for r in recs if not r["reached"])
    if by_lang:
        print(f"\n  unreached by language: {dict(by_lang)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
