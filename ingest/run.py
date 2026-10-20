"""Ingest a business website.

    python -m ingest.run https://someclinic.com.my --site someclinic
    python -m ingest.run --list

The crawl is cached per domain and chunk vectors per embedding model, so
re-ingesting hits neither the network nor the GPU-less embedder for text
it has seen. For a fast tuning loop, index a slice under its own label:

    python -m ingest.run https://someclinic.com.my --site clinic-dev --max-pages 20
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
from dotenv import load_dotenv

load_dotenv()

from .chunk import chunk_pages  # noqa: E402
from .crawl import Page, brand_of, crawl, split_contact_page  # noqa: E402
from .embed import EMBED_MODEL, embed_texts  # noqa: E402
from .store import connect, init_db, list_sites, replace_site  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = DATA_DIR / "pages"
VEC_CACHE = DATA_DIR / "vectors" / f"{EMBED_MODEL.replace('/', '_')}.npz"
EMBED_REPORT_EVERY = 64


def default_site_name(url: str) -> str:
    host = urlparse(url if url.startswith("http") else f"https://{url}").netloc
    return host.replace("www.", "").split(".")[0]


def cache_path(site: str) -> Path:
    return CACHE_DIR / f"{site}.json"


def load_pages(site: str) -> list[Page] | None:
    path = cache_path(site)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  cache unreadable ({exc}) — refetching", flush=True)
        return None
    return [Page(p["url"], p["title"], p["markdown"]) for p in raw["pages"]]


def save_pages(site: str, url: str, pages: list[Page]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path(site).write_text(
        json.dumps(
            {"site": site, "url": url, "fetched": time.strftime("%Y-%m-%d %H:%M:%S"),
             "pages": [vars(p) for p in pages]},
            ensure_ascii=False, indent=1,
        ),
        encoding="utf-8",
    )


def load_vectors() -> dict[str, np.ndarray]:
    if not VEC_CACHE.exists():
        return {}
    try:
        with np.load(VEC_CACHE) as z:
            return dict(zip(z["hashes"].tolist(), z["vectors"]))
    except (OSError, ValueError, KeyError):
        return {}


def save_vectors(cache: dict[str, np.ndarray]) -> None:
    VEC_CACHE.parent.mkdir(parents=True, exist_ok=True)
    np.savez(VEC_CACHE, hashes=np.array(list(cache)),
             vectors=np.stack(list(cache.values())))


def embed_with_progress(texts: list[str]) -> list[list[float]]:
    """Embed, reusing vectors for chunk text we have embedded before.

    Stage 3 tuning changes one knob and reverts if the numbers get worse.
    A revert re-chunks to byte-identical text, so it is a full cache hit
    and costs seconds instead of ten minutes on this CPU. Keyed by the
    embedding model, since its vectors are not interchangeable.
    """
    cache = load_vectors()
    keys = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]
    todo = [(k, t) for k, t in dict(zip(keys, texts)).items() if k not in cache]
    print(f"  embedding: {len(texts) - len(todo)} cached, {len(todo)} to compute",
          flush=True)

    started = time.time()
    for i in range(0, len(todo), EMBED_REPORT_EVERY):
        batch = todo[i : i + EMBED_REPORT_EVERY]
        for (k, _), vec in zip(batch, embed_texts([t for _, t in batch])):
            cache[k] = np.asarray(vec, dtype=np.float32)
        done = min(i + EMBED_REPORT_EVERY, len(todo))
        rate = done / max(time.time() - started, 1e-6)
        print(f"  embedded {done:>5}/{len(todo)}  "
              f"({rate:.1f}/s, ~{(len(todo)-done)/max(rate,1e-6):.0f}s left)",
              flush=True)

    if todo:
        save_vectors(cache)
    return [cache[k].tolist() for k in keys]


def main() -> int:
    ap = argparse.ArgumentParser(description="Crawl a site into the FAQ index")
    ap.add_argument("url", nargs="?", help="Business website URL")
    ap.add_argument("--site", help="Short name for this business (default: domain)")
    ap.add_argument("--max-pages", type=int, default=120)
    ap.add_argument("--list", action="store_true", help="Show indexed sites")
    ap.add_argument("--no-cache", action="store_true",
                    help="refetch from the network instead of data/pages/<site>.json")
    args = ap.parse_args()

    conn = connect()
    init_db(conn)

    if args.list:
        for name, count in list_sites(conn):
            print(f"{name:<24} {count:>5} chunks")
        return 0

    if not args.url:
        ap.error("url is required (or use --list)")

    site = args.site or default_site_name(args.url)
    started = time.time()

    # Keyed by domain, not by --site, so a throwaway label for a tuning run
    # ("--site sepa-dev --max-pages 20") reuses the crawl we already have.
    cache_key = default_site_name(args.url)
    pages = None if args.no_cache else load_pages(cache_key)
    if pages:
        print(f"\nusing cached crawl: {len(pages)} pages from {cache_path(cache_key)}",
              flush=True)
        print("  (--no-cache to refetch)", flush=True)
    else:
        print(f"\ncrawling {args.url}", flush=True)
        crawl_started = time.time()
        pages = crawl(args.url, max_pages=args.max_pages)
        if not pages:
            print("no readable pages found — site may be JS-rendered or blocking us")
            return 1
        print(f"  crawled {len(pages)} pages in {time.time()-crawl_started:.1f}s",
              flush=True)
        save_pages(cache_key, args.url, pages)
        print(f"  cached to {cache_path(cache_key)}", flush=True)

    if len(pages) > args.max_pages:
        # The chrome page is appended last by the crawler, so a plain slice
        # would drop it and a tuning run would wrongly conclude that
        # addresses and hours are unretrievable.
        chrome = [p for p in pages[args.max_pages:] if p.title == "Contact & Locations"]
        pages = pages[: args.max_pages] + chrome
        print(f"  limited to {len(pages)} pages (--max-pages)", flush=True)

    # Split the consolidated contact page into one page per branch. Done at
    # chunk time, not crawl time, so an existing cached crawl gets the fix
    # without a refetch.
    business = brand_of(pages)
    if business:
        print(f"  business name from page titles: {business}", flush=True)
    expanded: list[Page] = []
    for p in pages:
        expanded.extend(split_contact_page(p, business)
                        if p.title == "Contact & Locations" else [p])
    if len(expanded) != len(pages):
        print(f"  contact page split into {len(expanded) - len(pages) + 1} "
              f"per-branch pages", flush=True)
    pages = expanded

    chunks = chunk_pages(pages)
    print(f"\n{len(pages)} pages -> {len(chunks)} chunks", flush=True)

    vectors = embed_with_progress([c.embed_text() for c in chunks])
    stored = replace_site(conn, site, chunks, vectors)

    print(f"stored {stored} chunks under site '{site}' in {time.time()-started:.1f}s",
          flush=True)
    print(f"\ntry it:  python query.py --site {site}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
