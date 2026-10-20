"""Score the bot against an eval set.

    python evals/run.py --site sepadental
    python evals/run.py --site tiewdental --limit 5

Two headline numbers: accuracy on answerable questions, and the
false-answer rate — how often it invented something instead of escalating.
The second matters more.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from ingest.llm import generate, last_model, model_name  # noqa: E402
from ingest.store import connect  # noqa: E402
from query import answer  # noqa: E402

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

# The grader must not move when the bot model does. Before this existed,
# GEMINI_MODEL changed the bot AND the judge, so a model experiment was
# scored on a different yardstick than its own baseline. Pin it, and the
# judge also draws on a separate per-model daily quota.
JUDGE_MODEL = os.getenv("JUDGE_MODEL") or None
_LAST_JUDGE_MODEL = None

JUDGE_SYSTEM = """You grade one customer-service reply against one criterion.

You are NOT given the business's documents. Judge ONLY whether the reply
satisfies the criterion as written. Do not reason about whether the facts
are true — you cannot know that. If the criterion says a reply must refuse,
confirm, or avoid something, check exactly that.

Answer on the first line with a single word: PASS or FAIL.
On the second line give one short sentence of reason."""


# ------------------------------------------------------------ normalisation
def norm(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)                      # 2,000 -> 2000
    t = re.sub(r"(\d{1,2})[:.](\d{2})\s*([ap])\.?\s?m\.?", r"\1.\2\3m", t)
    t = re.sub(r"(?<![\d.:])(\d{1,2})\s*([ap])\.?\s?m\.?", r"\1.00\2m", t)
    return re.sub(r"\s+", " ", t).strip()


def contains(reply: str, needle: str) -> bool:
    r, n = norm(reply), norm(needle)
    if not n:
        return False
    if re.fullmatch(r"[\d.]+", n):        # bare number: must not be flanked by digits
        return re.search(rf"(?<![\d.,]){re.escape(n)}(?!\d)", r) is not None
    return n in r


# ------------------------------------------------------------------- judging
def judge(question: str, criterion: str, reply: str) -> tuple[bool, str]:
    global _LAST_JUDGE_MODEL
    _LAST_JUDGE_MODEL = None
    if not JUDGE_MODEL:
        raise RuntimeError("Set JUDGE_MODEL explicitly to grade semantic criteria")
    out = generate(JUDGE_SYSTEM,
                   f"CRITERION:\n{criterion}\n\n"
                   f"CUSTOMER QUESTION:\n{question}\n\nREPLY:\n{reply}", model=JUDGE_MODEL)
    _LAST_JUDGE_MODEL = last_model()
    first = out.strip().splitlines()[0].strip().upper() if out.strip() else ""
    reason = " ".join(out.strip().splitlines()[1:])[:200]
    return first.startswith("PASS"), reason or first


def grade(row: dict, result: dict) -> tuple[bool, list[str]]:
    """Return (passed, reasons-it-failed)."""
    ev = row.get("evaluation") or {}
    reply = result.get("reply", "")
    fails: list[str] = []

    for term in ev.get("required", []):
        if not contains(reply, term):
            fails.append(f"missing required {term!r}")
    any_terms = ev.get("required_any") or []
    if any_terms and not any(contains(reply, t) for t in any_terms):
        fails.append(f"none of required_any {any_terms}")
    for term in ev.get("forbidden", []):
        if contains(reply, term):
            fails.append(f"forbidden {term!r} present")
    pat = ev.get("forbidden_pattern")
    if pat and re.search(pat, norm(reply), re.I):
        fails.append(f"matched forbidden_pattern {pat!r}")

    # An answerable:false row is only correct if the bot actually escalated.
    if not row["answerable"] and result.get("answerable"):
        fails.append("answered a question the site cannot answer")

    if ev.get("semantic") and not fails:
        try:
            ok, why = judge(row["question"], ev["semantic"], reply)
        except Exception as exc:
            # A judge that cannot be reached is not evidence of a wrong
            # answer. Mark it and keep going rather than scoring it a fail.
            return None, [f"JUDGE UNAVAILABLE: {str(exc)[:90]}"]
        if not ok:
            fails.append(f"judge: {why}")
    return (not fails), fails


def retrieval_ok(row: dict, result: dict) -> bool | None:
    """Did the context actually carry the fact the row asks for?

    This used to compare URLs only. Every branch address on sepadental sat
    in one chunk filed under /contact-us, so a URL match said "retrieved"
    while the chunk the model saw held a different branch — 12 address rows
    were scored as generation failures when nothing had been retrieved.
    Check the required strings against the context text; fall back to the
    URL only when a row states no required strings.
    """
    ev = row.get("evaluation") or {}
    need = (ev.get("required") or []) + (ev.get("required_any") or [])
    # Must use contains()/norm(), not a raw substring: grade() matches the
    # reply through norm(), which folds "9 AM" and "9.00am" together. A raw
    # `in` here made the SAME needle pass grading and fail retrieval, so
    # four khengdental hours rows were reported as retrieval misses on text
    # that had in fact been retrieved.
    ctx = result.get("context") or ""
    if need and ctx:
        return (all(contains(ctx, t) for t in ev.get("required", []))
                and (not ev.get("required_any") or any(contains(ctx, t) for t in ev["required_any"])))
    targets = row.get("retrieval_target")
    if not targets:
        return None
    urls = " ".join(result.get("chunk_urls", []))
    return any(t in urls for t in targets)


# --------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--limit", type=int, help="score only the first N rows")
    ap.add_argument("--business", help="display name passed to the prompt")
    ap.add_argument("--rerun-fails", metavar="RESULTS.JSON",
                    help="score only the rows that failed in a previous "
                         "run — concentrates a small quota on the rows "
                         "that actually discriminate between models")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    rows = yaml.safe_load((HERE / f"{args.site}.yaml").read_text(encoding="utf-8"))
    if args.rerun_fails:
        prev = json.loads(Path(args.rerun_fails).read_text(encoding="utf-8"))
        prev = prev["rows"] if isinstance(prev, dict) else prev
        failed = {r["question"] for r in prev if r["passed"] is False}
        rows = [r for r in rows if r["question"] in failed]
        print(f"re-running {len(rows)} rows that failed in "
              f"{Path(args.rerun_fails).name}", flush=True)
    if args.limit:
        rows = rows[: args.limit]
    conn = connect()
    started = time.time()
    records = []

    print(f"scoring {len(rows)} rows for '{args.site}' via {model_name()}\n", flush=True)
    errors = 0
    for i, row in enumerate(rows, 1):
        global _LAST_JUDGE_MODEL
        _LAST_JUDGE_MODEL = None
        bot_model = None
        try:
            result = answer(conn, args.site, row["question"], args.business)
            bot_model = last_model()
            passed, fails = grade(row, result)
        except Exception as exc:
            result = {"answerable": None, "reply": f"<ERROR {exc}>", "chunk_urls": []}
            passed, fails = None, [f"API ERROR: {str(exc)[:90]}"]
        if passed is None:
            errors += 1
            if errors >= 5:
                print(f"\n  ABORTING: {errors} unscoreable rows in a row — the "
                      f"provider is refusing calls. Scored {i - errors} of "
                      f"{len(rows)} before this.\n  last: {fails[0]}", flush=True)
                break
        else:
            errors = 0
        rec = {**{k: row[k] for k in ("question", "lang", "answer_type",
                                      "answerable", "severity")},
               "passed": passed, "fails": fails,
               "reply": result.get("reply", ""),
               "bot_answerable": result.get("answerable"),
               "top_score": result.get("top_score"),
               "retrieval_ok": retrieval_ok(row, result),
               # Which model actually answered. A silent fallback onto the
               # judge's own model makes a row self-graded, and nothing
               # else in the record would show it.
               "model_used": bot_model,
               "judge_model_used": _LAST_JUDGE_MODEL,
               "chunk_urls": result.get("chunk_urls", [])}
        records.append(rec)
        label = "ERR " if passed is None else ("PASS" if passed else "FAIL")
        print(f"  [{i:>3}/{len(rows)}] {label}  "
              f"{row['lang']:<6} {row['question'][:44]}", flush=True)
        # checkpoint, so a provider outage never loses completed rows
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / f".{args.site}-partial.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")

    report(args.site, records, time.time() - started)
    return 0


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "n/a"


def report(site: str, all_recs: list[dict], elapsed: float) -> None:
    # Rows the provider refused are not evidence either way — keep them out
    # of every rate, and say how many there were.
    errored = [r for r in all_recs if r["passed"] is None]
    recs = [r for r in all_recs if r["passed"] is not None]
    if not recs:
        print("\n  nothing scoreable — every row errored")
        for r in errored[:3]:
            print(f"    {r['fails'][0]}")
        return
    answerable = [r for r in recs if r["answerable"]]
    unanswerable = [r for r in recs if not r["answerable"]]
    acc = sum(r["passed"] for r in answerable)
    # A false answer = the site cannot answer, and the bot answered anyway
    # or emitted something forbidden.
    false_answers = [r for r in unanswerable if not r["passed"]]
    critical_fa = [r for r in false_answers if r["severity"] == "critical"]
    refused_ok = sum(r["passed"] for r in unanswerable)

    print("\n" + "=" * 74)
    print(f"  {site}   {len(recs)} scored   {elapsed:.0f}s   {model_name()}")
    print(f"  judge: {JUDGE_MODEL or model_name()}")
    if errored:
        print(f"  {len(errored)} rows unscoreable (provider errors) — excluded")
        print(f"  NOTE: scored on {len(recs)} of {len(all_recs)} rows"
              f" — not comparable to a full run")
    # A row the bot answered with the judge's own model was graded by the
    # model that wrote it. Rare (it needs a fallback) but invisible without
    # this, and it silently reintroduces the self-marking JUDGE_MODEL exists
    # to prevent.
    if JUDGE_MODEL:
        selfgraded = [r for r in recs if r.get("model_used") == JUDGE_MODEL]
        if selfgraded:
            print(f"  WARNING: {len(selfgraded)} rows fell back onto the judge's "
                  f"own model ({JUDGE_MODEL}) and are self-graded")
            for r in selfgraded[:5]:
                print(f"      [{r['lang']}] {r['question'][:56]}")
    print("=" * 74)
    # Two different things, previously counted as one:
    #   fabricated  — a forbidden fact actually reached the customer. This is
    #                 the number a business owner cares about.
    #   flag wrong  — the reply invented nothing but set answerable true, so no
    #                 human would be alerted. Matters for handoff, not for trust.
    fabricated = [r for r in recs
                  if any("forbidden" in f for f in r["fails"])]
    flag_wrong = [r for r in unanswerable
                  if any("cannot answer" in f for f in r["fails"])
                  and not any("forbidden" in f for f in r["fails"])]
    print(f"  ACCURACY on answerable  {acc}/{len(answerable)}  "
          f"({pct(acc, len(answerable))})")
    print(f"  FABRICATED A FACT       {len(fabricated)}/{len(recs)}  "
          f"({pct(len(fabricated), len(recs))})   <-- the one that matters")
    print(f"  escalation flag wrong   {len(flag_wrong)}/{len(unanswerable)}  "
          f"({pct(len(flag_wrong), len(unanswerable))})  (safe reply, no handoff)")
    print(f"  combined false-answer   {len(false_answers)}/{len(unanswerable)}  "
          f"({pct(len(false_answers), len(unanswerable))})   "
          f"critical: {len(critical_fa)}")
    print(f"  refusal accuracy        {refused_ok}/{len(unanswerable)}  "
          f"({pct(refused_ok, len(unanswerable))})")
    checked = [r for r in recs if r["retrieval_ok"] is not None]
    missed = [r for r in checked if not r["retrieval_ok"]]
    print(f"  retrieval failures      {len(missed)}/{len(checked)}  "
          f"({pct(len(missed), len(checked))})")

    for label, key in (("BY LANGUAGE", "lang"), ("BY TYPE", "answer_type")):
        print(f"\n  {label}")
        groups = defaultdict(list)
        for r in recs:
            groups[r[key]].append(r)
        for k, g in sorted(groups.items()):
            p = sum(x["passed"] for x in g)
            print(f"    {k:<20} {p:>3}/{len(g):<3} {pct(p, len(g)):>5}")

    if any(not r["passed"] for r in recs):
        print("\n  FAILURES")
        order = {"critical": 0, "high": 1, "normal": 2}
        for r in sorted((x for x in recs if not x["passed"]),
                        key=lambda x: order[x["severity"]]):
            print(f"\n    [{r['severity']}] {r['lang']}  {r['question']}")
            print(f"      why:   {'; '.join(r['fails'])}")
            print(f"      reply: {' '.join(r['reply'].split())[:150]}")
            print(f"      top rerank score: {r['top_score']}   "
                  f"retrieval_ok: {r['retrieval_ok']}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = RESULTS / f"{site}-{stamp}.json"
    path.write_text(json.dumps({
        "site": site, "model": model_name(),
        "judge_model": JUDGE_MODEL or model_name(),
        "elapsed_s": round(elapsed, 1),
        "accuracy": {"passed": acc, "total": len(answerable)},
        "false_answers": {"count": len(false_answers),
                          "critical": len(critical_fa),
                          "total_unanswerable": len(unanswerable)},
        "rows": all_recs, "errored": len(errored),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n  saved {path}")


if __name__ == "__main__":
    sys.exit(main())
