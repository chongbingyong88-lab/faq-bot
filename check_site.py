"""Qualify a candidate site before committing to it. No API calls.

    python check_site.py https://candidate.com.my
    python check_site.py url1 url2 url3

Stage 0 of PLAN.md: everything downstream is tuned for whichever sites you
pick, so switching later costs Stage 3 again. Two things disqualify a site
and both are cheap to test:

    thin      under ~150 extracted words on the homepage — JS-rendered,
              or a one-pager with nothing to answer from
    small     fewer than ~15 crawlable pages

It reuses the real extractor, so "extracted words" means what the ingester
would actually get, not what a browser renders. A site that looks rich but
extracts nothing is exactly the failure this is meant to catch.
"""
from __future__ import annotations

import sys
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from ingest.crawl import UA, _extract, _links, _sitemap_urls, _worth_fetching

MIN_WORDS = 150
MIN_PAGES = 15


def check(url: str) -> bool:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    # Must match crawl(), which strips www. A site whose internal links use
    # the bare domain looks like it has zero pages otherwise — sepadental
    # reported 0 here while the crawler was happily fetching 70 of them.
    host = urlparse(url).netloc.replace("www.", "")
    print(f"\n{url}")

    with httpx.Client(follow_redirects=True, timeout=20.0,
                      headers={"User-Agent": UA}) as client:
        try:
            r = client.get(url)
            r.raise_for_status()
        except Exception as exc:
            print(f"  UNREACHABLE  {str(exc)[:70]}")
            return False

        page = _extract(url, r.content)
        words = page.words if page else 0
        headings = (page.markdown.count("\n#") if page else 0)

        urls = _sitemap_urls(url, client, host)
        source = "sitemap"
        if not urls:
            # No sitemap is common on small sites; fall back to homepage links
            # so a good site is not rejected for a missing xml file.
            urls = [u for u in _links(url, r.content, host)
                    if _worth_fetching(u, host)]
            source = "homepage links"

    print(f"  homepage extract   {words} words, {headings} headings")
    print(f"  pages found        {len(urls)} ({source})")

    thin = words < MIN_WORDS
    small = len(urls) < MIN_PAGES
    if thin:
        print(f"  THIN — under {MIN_WORDS} words. JS-rendered or a one-pager.")
    if small:
        print(f"  SMALL — under {MIN_PAGES} pages. Little to answer from.")
    ok = not (thin or small)
    print(f"  VERDICT            {'PASS' if ok else 'SKIP'}")
    return ok


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    results = [(u, check(u)) for u in sys.argv[1:]]
    passed = [u for u, ok in results if ok]
    print(f"\n{len(passed)}/{len(results)} passed")
    for u in passed:
        print(f"  {u}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
