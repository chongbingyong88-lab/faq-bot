"""Split page markdown into retrieval chunks.

Two things this has to get right, both learned the hard way:

1. Headings travel with their body text. "RM150" alone is useless;
   "Scaling & Polishing > Price: RM150" retrieves and answers correctly.
2. Short sections are PACKED, not dropped. Real FAQ and price-list pages
   are full of 10-word sections, and those are the highest-value content
   on the site. A naive minimum-length filter deletes exactly the answers
   customers ask for.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .crawl import Page

HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
TARGET_WORDS = 180
OVERLAP_WORDS = 40
MIN_CHUNK_WORDS = 8
# Don't start a new chunk on a section change until we've accumulated at
# least this much. FAQ pages are often 20 tiny sections in a row; breaking
# on every one of them produces chunks too small to retrieve well.
MIN_PACK_WORDS = 60


def _similar(a: str, b: str) -> bool:
    """True if one string is essentially contained in the other."""
    a2, b2 = a.strip().lower(), b.strip().lower()
    if not a2 or not b2:
        return False
    return a2 in b2 or b2 in a2


@dataclass
class Chunk:
    url: str
    title: str
    heading: str
    text: str

    def embed_text(self) -> str:
        """What we actually embed — context prefix included.

        The page title and the H1 are usually near-duplicates, so we drop
        the title when it just repeats the heading.
        """
        parts: list[str] = []
        head_root = self.heading.split(" > ")[0] if self.heading else ""
        if self.title and not _similar(self.title, head_root):
            parts.append(self.title)
        if self.heading:
            parts.append(self.heading)
        prefix = " > ".join(parts)
        return f"{prefix}\n{self.text}" if prefix else self.text


def _split_sections(markdown: str) -> list[tuple[str, str]]:
    """Return (heading_path, body) pairs in document order."""
    sections: list[tuple[str, str]] = []
    stack: list[str] = []
    buf: list[str] = []
    current = ""

    def flush(heading: str):
        body = "\n".join(buf).strip()
        if body or heading:
            sections.append((heading, body))

    for line in markdown.splitlines():
        m = HEADING.match(line.strip())
        if m:
            flush(current)
            buf = []
            level = len(m.group(1))
            text = m.group(2).strip()
            stack = stack[: level - 1]
            stack.append(text)
            current = " > ".join(stack)
        else:
            buf.append(line)
    flush(current)

    return [s for s in sections if s[1] or s[0]] or [("", markdown.strip())]


def _windows(body: str) -> list[str]:
    """Sliding window for sections longer than the target size."""
    words = body.split()
    if len(words) <= TARGET_WORDS:
        return [body]

    out: list[str] = []
    step = TARGET_WORDS - OVERLAP_WORDS
    for start in range(0, len(words), step):
        piece = words[start : start + TARGET_WORDS]
        if len(piece) < MIN_CHUNK_WORDS and out:
            break
        out.append(" ".join(piece))
    return out


def _group_offset(sections: list[tuple[str, str]]) -> int:
    """Most pages wrap everything in a single H1 (the page title).

    Grouping on that root would put the whole page in one chunk, so if
    every section shares the same first heading component, we group one
    level deeper instead.
    """
    roots = {h.split(" > ")[0] for h, _ in sections if h}
    return 1 if len(roots) == 1 else 0


def _group_key(heading: str, offset: int) -> str:
    if not heading:
        return ""
    parts = heading.split(" > ")
    return parts[offset] if len(parts) > offset else parts[-1]


def chunk_page(page: Page) -> list[Chunk]:
    """Pack sections into chunks, splitting only what is genuinely long."""
    chunks: list[Chunk] = []
    buf_parts: list[str] = []
    buf_words = 0
    buf_heading = ""

    def flush():
        nonlocal buf_parts, buf_words, buf_heading
        if buf_parts:
            text = "\n\n".join(buf_parts).strip()
            if len(text.split()) >= MIN_CHUNK_WORDS:
                chunks.append(
                    Chunk(page.url, page.title, buf_heading, text)
                )
        buf_parts, buf_words, buf_heading = [], 0, ""

    sections = _split_sections(page.markdown)
    offset = _group_offset(sections)

    for heading, body in sections:
        words = len(body.split())

        # Long section: flush the buffer, then window it on its own.
        if words > TARGET_WORDS:
            flush()
            for w in _windows(body):
                chunks.append(Chunk(page.url, page.title, heading, w.strip()))
            continue

        # Start a new chunk when the buffer is full or we've moved into a
        # different section of the page.
        section_changed = _group_key(heading, offset) != _group_key(buf_heading, offset)
        if buf_parts and (
            buf_words + words > TARGET_WORDS
            or (section_changed and buf_words >= MIN_PACK_WORDS)
        ):
            flush()

        if not buf_parts:
            buf_heading = heading

        # Keep the sub-heading inline so packed sections stay distinguishable.
        leaf = heading.split(" > ")[-1] if heading else ""
        piece = f"{leaf}: {body}" if leaf and body else (body or leaf)
        if piece.strip():
            buf_parts.append(piece.strip())
            buf_words += words

    flush()
    return chunks


def chunk_pages(pages: list[Page]) -> list[Chunk]:
    out: list[Chunk] = []
    for p in pages:
        out.extend(chunk_page(p))
    return out
