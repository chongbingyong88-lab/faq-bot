"""Print stored chunks for a site so a human can read them.

    python dump_chunks.py --site sepadental --limit 30 --grep RM --with-heading
    python dump_chunks.py --site tiewdental --url-like /services/ --out f.txt
"""
from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

load_dotenv()

from ingest.store import connect  # noqa: E402


def build_query(args) -> tuple[str, list]:
    where, params = ["site = %s"], [args.site]
    if args.grep:
        where.append("content ILIKE %s")
        params.append(f"%{args.grep}%")
    if args.regex:
        # Case-sensitive POSIX regex: 'RM ?[0-9]' finds prices, where a
        # plain 'RM' substring also matches "form" and "warm".
        where.append("content ~ %s")
        params.append(args.regex)
    if args.url_like:
        where.append("url ILIKE %s")
        params.append(f"%{args.url_like}%")
    if args.not_url_like:
        where.append("url NOT ILIKE %s")
        params.append(f"%{args.not_url_like}%")
    if args.with_heading:
        where.append("heading <> ''")
    cond = " AND ".join(where)

    if args.spread:
        # One chunk per page, position chosen at random. Ordering by
        # position instead hands back chunk #1 of every page, and chunk #1
        # is the pre-heading preamble - on some sites that is the same
        # cookie/chat widget on every page, which tells you nothing.
        sql = ("SELECT url, heading, content FROM ("
               " SELECT url, heading, content,"
               " ROW_NUMBER() OVER (PARTITION BY url ORDER BY random()) rn"
               f" FROM chunks WHERE {cond}) t WHERE rn = 1 ORDER BY url LIMIT %s")
    else:
        sql = (f"SELECT url, heading, content FROM chunks WHERE {cond} "
               "ORDER BY id LIMIT %s")
    return sql, params + [args.limit]


def main() -> int:
    ap = argparse.ArgumentParser(description="Dump stored chunks for reading")
    ap.add_argument("--site", required=True)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--grep", help="only chunks whose text contains this")
    ap.add_argument("--regex", help="only chunks whose text matches this regex")
    ap.add_argument("--url-like", help="only chunks from urls matching this")
    ap.add_argument("--not-url-like", help="skip chunks from urls matching this")
    ap.add_argument("--with-heading", action="store_true",
                    help="skip chunks that have no heading path")
    ap.add_argument("--spread", action="store_true", help="one chunk per page")
    ap.add_argument("--out", help="write here instead of stdout")
    args = ap.parse_args()

    conn = connect()
    sql, params = build_query(args)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print(f"no chunks matched for site '{args.site}'", file=sys.stderr)
        return 1
    total, pages = conn.execute("SELECT COUNT(*), COUNT(DISTINCT url) "
        "FROM chunks WHERE site=%s", (args.site,)).fetchone()

    out = [f"site '{args.site}': {total} chunks / {pages} pages - showing {len(rows)}"]
    for i, (url, heading, content) in enumerate(rows, 1):
        out += ["\n" + "=" * 72, f"[{i}] {url}",
                f"    heading: {heading or '(none)'}   ({len(content.split())} words)",
                "-" * 72, content]
    text = "\n".join(out)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"wrote {len(rows)} chunks to {args.out}")
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
