"""Crawl a business website and extract clean text per page.

Strategy: try sitemap.xml first (fast, complete), fall back to a
breadth-first crawl from the homepage. Same host only.
"""
from __future__ import annotations

import re
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import zip_longest
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
import trafilatura
from lxml import etree, html as lxml_html

SKIP_EXT = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".zip", ".mp4", ".mp3", ".css", ".js", ".xml", ".json",
)
SKIP_PATTERNS = re.compile(
    r"/(wp-admin|wp-login|cart|checkout|account|login|signin|feed|tag/|author/)",
    re.I,
)
UA = "Mozilla/5.0 (compatible; FAQBotIngest/0.1)"
CONCURRENCY = 8
# Minimum gap between request starts, shared across all workers. Eight
# sockets against a small clinic's shared hosting is rude without it.
HOST_DELAY = 0.15


class _Throttle:
    """Rate-limit request starts across every worker thread."""

    def __init__(self, delay: float):
        self.delay = delay
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            pause = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.delay
        if pause:
            time.sleep(pause)

# Containers that are navigation or chrome on every site we've seen.
DROP_CLASS = re.compile(
    r"(nav|menu|breadcrumb|header|footer|sidebar|social|cookie|popup)", re.I
)
BLOCK_TAGS = "//h1|//h2|//h3|//h4|//h5|//h6|//p|//li|//td|//th"
NESTED_BLOCK = ".//p|.//li|.//h1|.//h2|.//h3|.//h4|.//h5|.//h6|.//td|.//th"


@dataclass
class Page:
    url: str
    title: str
    markdown: str

    @property
    def words(self) -> int:
        return len(self.markdown.split())


def _normalise(url: str) -> str:
    url, _ = urldefrag(url)
    return url.rstrip("/") or url


def _worth_fetching(url: str, host: str) -> bool:
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False
    if p.netloc.replace("www.", "") != host:
        return False
    if url.lower().endswith(SKIP_EXT):
        return False
    if SKIP_PATTERNS.search(p.path):
        return False
    return True


def _interleave(groups: list[list[str]]) -> list[str]:
    """Round-robin across sub-sitemaps instead of concatenating them.

    Sitemap indexes tend to list the biggest, least useful section first:
    a Yoast site puts 281 blog posts ahead of the 13 service pages. Taken
    in document order, any sane page budget is spent entirely on the blog
    and never reaches the pages customers actually ask about — prices,
    hours, branches. Interleaving spreads the budget over every section,
    so small high-value sections are exhausted early rather than starved.
    """
    out: list[str] = []
    seen: set[str] = set()
    for row in zip_longest(*groups):
        for url in row:
            if url and url not in seen:
                seen.add(url)
                out.append(url)
    return out


def _sitemap_urls(base: str, client: httpx.Client, host: str) -> list[str]:
    """Pull URLs from sitemap.xml, following sitemap index files one level."""
    groups: list[list[str]] = []
    queue = [urljoin(base, "/sitemap.xml"), urljoin(base, "/sitemap_index.xml")]
    seen_maps: set[str] = set()

    while queue:
        sm = queue.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        try:
            r = client.get(sm)
            if r.status_code != 200:
                continue
            root = etree.fromstring(r.content)
        except Exception:
            continue

        tag = etree.QName(root).localname
        locs = [
            e.text.strip()
            for e in root.iter()
            if etree.QName(e).localname == "loc" and e.text
        ]
        if tag == "sitemapindex":
            queue.extend(locs[:20])
        else:
            group = [u for u in locs if _worth_fetching(u, host)]
            if group:
                groups.append(group)

    return _interleave(groups)


def _links(page_url: str, body: bytes, host: str) -> list[str]:
    try:
        doc = lxml_html.fromstring(body)
    except Exception:
        return []
    out = []
    for href in doc.xpath("//a/@href"):
        full = _normalise(urljoin(page_url, href))
        if _worth_fetching(full, host):
            out.append(full)
    return out


DEDUPE_MIN = 40      # only long text is boilerplate; short cells legitimately repeat
ROW_MAX_CHARS = 140  # a table row, not a paragraph
HAS_DIGIT = re.compile(r"\d")


def _cells(el) -> list[str] | None:
    """The row this element represents, or None if it is not one.

    Price tables on Framer/Elementor sites are divs, not <table>, so there
    is no <tr> to key off. What survives is the grouping: a row is a small
    element whose direct children each hold one short cell of text, with a
    number somewhere among them.

    Recovering the row matters twice over. It keeps a treatment attached to
    its price when a chunk boundary lands mid-table, and it keeps the two
    price columns ("Standard" and "Signature", different amounts for the
    same treatment) distinguishable instead of two bare numbers in a row.
    """
    kids = [k for k in el if isinstance(k.tag, str)]
    if not 2 <= len(kids) <= 4:
        return None
    out = []
    for k in kids:
        t = " ".join(" ".join(k.itertext()).split())
        if not t or len(t) > 70:
            return None
        out.append(t)
    joined = " ".join(out)
    if len(joined) > ROW_MAX_CHARS or not HAS_DIGIT.search(joined):
        return None
    # First cell is the label; at least one of the rest must carry a number.
    if not any(HAS_DIGIT.search(c) for c in out[1:]):
        return None
    return out


def _structural_text(body: bytes) -> str:
    """Read the DOM directly, keeping headings as markdown.

    Only used when trafilatura has thrown the real content away — see
    _needs_fallback. Deliberately dumber than trafilatura: strip the
    obvious chrome, then take every leaf block element in order.
    """
    # lxml infers the encoding from the bytes and gets it wrong on a page that
    # declares no charset: khengdental stored "Mon â\x80\x94 Fri" — utf-8 read
    # as latin-1 — in 44% of its chunks, em dashes and all. Decode first when
    # the bytes really are utf-8, which modern pages are, and only fall back to
    # lxml's guess when they are not.
    try:
        doc = lxml_html.fromstring(body.decode("utf-8"))
    except UnicodeDecodeError:
        try:
            doc = lxml_html.fromstring(body)
        except Exception:
            return ""
    except Exception:
        return ""

    for el in doc.xpath("//script|//style|//noscript|//nav|//header|//footer|//form"):
        if el.getparent() is not None:
            el.getparent().remove(el)
    for el in doc.xpath("//*[@class]"):
        if DROP_CLASS.search(el.get("class") or "") and el.getparent() is not None:
            el.getparent().remove(el)

    # Rows first, so a treatment and its price leave as one line. Everything
    # inside a row is then off-limits to the leaf walk below.
    rows: dict = {}
    consumed: set = set()
    for el in doc.iter():
        if not isinstance(el.tag, str) or el in consumed:
            continue
        cells = _cells(el)
        if cells:
            rows[el] = cells
            for d in el.iterdescendants():
                consumed.add(d)

    lines: list[str] = []
    seen: set[str] = set()
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        if el in rows:
            label, *values = rows[el]
            lines.append(f"{label}: {' | '.join(values)}" if values else label)
            continue
        if el in consumed:
            continue
        if not re.fullmatch(r"h[1-6]|p|li|td|th", el.tag):
            continue
        # Containers repeat their children's text; take leaves only.
        if el.xpath(NESTED_BLOCK):
            continue
        # itertext, not text_content: inline <span>/<br> markup splits
        # "Saturday: 9.30am" runs, and joining without a separator would
        # weld them into "5.30pmSaturday".
        text = " ".join(" ".join(el.itertext()).split())
        if len(text) < 3:
            continue
        # Dedupe boilerplate only. Keying on exact text at any length deleted
        # every repeat of a short value, so a price table lost the second
        # "80" and every later row inherited the wrong number.
        if len(text) >= DEDUPE_MIN:
            if text in seen:
                continue
            seen.add(text)
        if el.tag[0] == "h" and el.tag[1:].isdigit():
            lines.append(f"{'#' * int(el.tag[1])} {text}")
        else:
            lines.append(text)
    return "\n".join(lines)


def _needs_fallback(md: str | None, body: bytes) -> bool:
    """True when trafilatura's output looks like it lost the page.

    Two tells, both seen on real Malaysian clinic sites: almost nothing
    came back at all (branch pages, where the address and opening hours
    live in an Elementor icon-list), or prose came back with every
    heading stripped (whole sites where headings are widget markup).
    """
    if not md or len(md.split()) < 60:
        return True
    if any(line.lstrip().startswith("#") for line in md.splitlines()):
        return False
    try:
        doc = lxml_html.fromstring(body)
    except Exception:
        return False
    return len(doc.xpath("//h1|//h2|//h3|//h4|//h5|//h6")) >= 2


def _extract(url: str, body: bytes) -> Page | None:
    """trafilatura strips nav/footer/ads and gives us headings as markdown."""
    md = trafilatura.extract(
        body,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        favor_precision=True,
        url=url,
    )
    if _needs_fallback(md, body):
        alt = _structural_text(body)
        if len(alt.split()) > (len(md.split()) if md else 0):
            md = alt
    if not md or len(md.split()) < 25:
        return None

    title = ""
    meta = trafilatura.extract_metadata(body)
    if meta and meta.title:
        title = meta.title.strip()

    return Page(url=url, title=title, markdown=md)


BOILERPLATE_RATIO = 0.5
MIN_PAGES_FOR_DEDUPE = 8
# Where the one surviving copy of the chrome gets filed, best match first.
# A specific branch page must never win: filing twelve branch addresses
# under /branches/publika would have the bot cite Publika for Seremban.
CONTACT_URL = (
    re.compile(r"/contact", re.I),
    re.compile(r"/(branches|locate-us|locations|find-us)/?$", re.I),
)


def _chrome_span(lines: list[str], boiler: set[str]) -> tuple[int, int]:
    """Where a page's real content starts and ends.

    Header and footer chrome is a contiguous run of repeated blocks at
    the top and bottom of the page. Everything between those runs is the
    page's own content, even if some of it also appears elsewhere.
    """
    n = len(lines)
    head = 0
    while head < n and (not lines[head].strip() or lines[head].strip() in boiler):
        head += 1
    tail = n
    while tail > head and (not lines[tail - 1].strip() or lines[tail - 1].strip() in boiler):
        tail -= 1
    return head, tail


def _chrome_page(pages: list[Page], base_url: str, blocks: list[str]) -> Page:
    """File the site-wide chrome on one page instead of all of them.

    The footer is where sites like SEPA keep every branch address, so
    deleting it outright would lose the only copy of the thing customers
    ask for most. It is attributed to the site's real contact/branches
    URL when the crawl found one, so an answer drawn from it cites a page
    that actually exists.
    """
    url = None
    for pattern in CONTACT_URL:
        url = next((p.url for p in pages if pattern.search(urlparse(p.url).path)), None)
        if url:
            break
    return Page(
        url=url or urljoin(base_url, "/contact-us"),
        title="Contact & Locations",
        markdown="# Contact & Locations\n" + "\n".join(blocks),
    )


POSTCODE = re.compile(r"\b\d{5}\b")
PHONE_LINE = re.compile(r"^\+?[\d][\d\s\-()]{6,}$")

# Malaysian states, plus the abbreviations sites actually write. Used only to
# group branches so "any clinic in Johor" reaches one chunk instead of four.
STATES = (
    ("Kuala Lumpur", ("kuala lumpur", " kl", ",kl")),
    ("Selangor", ("selangor",)),
    ("Johor", ("johor",)),
    ("Negeri Sembilan", ("negeri sembilan",)),
    ("Penang", ("penang", "pulau pinang")),
    ("Perak", ("perak",)),
    ("Melaka", ("melaka", "malacca")),
    ("Pahang", ("pahang",)),
    ("Kedah", ("kedah",)),
    ("Kelantan", ("kelantan",)),
    ("Terengganu", ("terengganu",)),
    ("Perlis", ("perlis",)),
    ("Sabah", ("sabah",)),
    ("Sarawak", ("sarawak",)),
    ("Putrajaya", ("putrajaya",)),
    ("Labuan", ("labuan",)),
)


def _state_of(address: str) -> str | None:
    """Canonical state for an address line, or None.

    One site writes the same state three ways — "Johor Darul Ta'zim",
    "Johor Bahru", "81300, Skudai, Johor" — so grouping on the raw tail
    would scatter three branches into three groups.
    """
    low = " " + address.lower()
    for name, markers in STATES:
        if any(m in low for m in markers):
            return name
    return None


_STREET = re.compile(r"^(?:jalan|jln\.?|lorong|lrg\.?|persiaran|lebuh(?:raya)?)\b",
                     re.I)
_UNIT_HEAD = re.compile(r"^(?:no\.?\s|lot\s|unit\s|suite\s|blok\s|\d+[a-z]?\b)", re.I)
_UNIT_WORD = re.compile(r"\b(?:floor|tingkat|aras|basement)\b", re.I)


def _is_unit(part: str) -> bool:
    return bool(_UNIT_HEAD.match(part) or _UNIT_WORD.search(part))


def _branch_label(address: str) -> str:
    """A locality heading for an address line.

    The fallback used to be `address.split(",")[0]`, which on a Malaysian
    address is the lot number: "No 32A (Ground Floor)". As a heading that
    carries no locality at all, so the chunk lost every "alamat klinik sepa
    puchong" to boilerplate that at least said the brand — the real address
    sat at rank 25 while twenty-four "Welcome to SEPA Dental" chunks
    outranked it.

    Anchor on the postcode instead: the segment holding it names the town,
    and the nearest segment before it that is neither a street nor a unit
    names the neighbourhood. "Kepayang Sentral, Seremban" beats
    "12 (Ground Floor)" as both a heading and an embedding.
    """
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if not parts:
        return address.strip()

    post = next((i for i, p in enumerate(parts) if POSTCODE.search(p)), None)
    if post is None:
        town, town_i = parts[-1], len(parts) - 1
    else:
        town, town_i = POSTCODE.sub("", parts[post]).strip(" .-"), post
        # Some sites write "81300, Skudai, Johor" — the postcode stands alone
        # and the town is the segment after it.
        if not town and post + 1 < len(parts):
            town_i = post + 1
            town = parts[town_i]

    area = ""
    for p in reversed(parts[:town_i]):
        if _is_unit(p) or _STREET.match(p):
            continue
        area = p
        break

    if area and area.lower() != town.lower():
        return f"{area}, {town}" if town else area
    return town or parts[0]


_TITLE_SEP = re.compile(r"\s*[|–—]\s*|\s+-\s+")


def brand_of(pages: list[Page]) -> str:
    """The business name as the site itself writes it, or "".

    Most sites suffix every title with their name — "Contact Us | SEPA
    Dental". Branch chunks need that name: a bare address answers "alamat
    klinik sepa puchong" on one word of four, while every boilerplate block
    on the site says both "SEPA" and "dental clinic", so boilerplate wins.

    Sites that never write their own name in a title return "" and keep the
    previous behaviour — which is fine, because a site whose contact page
    pairs branch labels to addresses already has a real name per branch.
    """
    counts: Counter[str] = Counter()
    for p in pages:
        for part in _TITLE_SEP.split(p.title or ""):
            part = part.strip()
            if 2 <= len(part.split()) <= 4:
                counts[part] += 1
    if not counts:
        return ""
    name, seen = counts.most_common(1)[0]
    return name if seen >= 3 else ""


def split_contact_page(page: Page, business: str = "") -> list[Page]:
    """One page per branch address, so each keeps its own locality words.

    Consolidating the footer put all twelve SEPA addresses in a single
    chunk. That chunk's embedding averaged twelve different localities, so
    "where is your TTDI clinic" ranked it 10th of 60 and the model —
    correctly — answered that it did not have the address. Retrieval was
    scored as a hit because the chunk's URL matched, which hid it.

    Split per address and "TTDI Plaza, Jalan Wan Kadir 3" becomes a chunk of
    its own. Branch labels are paired by position when the page lists one
    label per phone number and the counts agree; otherwise each address
    still becomes its own chunk, which is the part that matters.

    Splitting alone loses the questions the blob was good at: "any clinic in
    Johor" wants four addresses at once and no per-branch chunk holds them.
    So each state also gets one grouped page. Specific questions match the
    per-branch chunk, regional ones match the group.
    """
    lines = [ln.strip() for ln in page.markdown.splitlines() if ln.strip()]
    body = [ln for ln in lines if not ln.startswith("#")]

    addrs: list[int] = []          # index of each address line
    labels: list[int] = []         # index of each "name above a phone" line
    for i, ln in enumerate(body):
        nxt = body[i + 1] if i + 1 < len(body) else ""
        if POSTCODE.search(ln) and len(ln.split()) >= 4:
            addrs.append(i)
        elif (PHONE_LINE.match(nxt) and not PHONE_LINE.match(ln)
              and len(ln.split()) >= 2 and not POSTCODE.search(ln)):
            labels.append(i)

    if len(addrs) < 2:
        return [page]

    paired = [body[i] for i in labels] if len(labels) == len(addrs) else []

    used: set[int] = set()
    out: list[Page] = []
    records: list[tuple[str, str]] = []          # (name, "addr phone")
    for n, i in enumerate(addrs):
        used.add(i)
        parts: list[str] = []
        if paired:
            parts.append(paired[n])
            used.add(labels[n])
            if labels[n] + 1 < len(body) and PHONE_LINE.match(body[labels[n] + 1]):
                parts.append(body[labels[n] + 1])
                used.add(labels[n] + 1)
        parts.append(body[i])
        if i + 1 < len(body) and PHONE_LINE.match(body[i + 1]):
            parts.append(body[i + 1])
            used.add(i + 1)
        name = paired[n] if paired else _branch_label(body[i])
        # Say the brand once per branch, unless the label already does. An
        # address alone shares no words with "klinik sepa puchong" beyond
        # the locality, and loses to boilerplate that says the name.
        if business and business.lower() not in name.lower():
            head = f"{business} — {name}"
            lead = [f"{business} clinic branch in {name}."]
        else:
            head, lead = name, []
        out.append(Page(url=page.url, title=page.title,
                        markdown="# " + head + "\n" + "\n".join(lead + parts)))
        records.append((name, " ".join(parts[-2:] if len(parts) > 1 else parts)))

    # One page per state, so a regional question reaches every branch in it.
    groups: dict[str, list[str]] = {}
    for (name, detail), i in zip(records, addrs):
        state = _state_of(body[i])
        if state:
            groups.setdefault(state, []).append(f"{name}: {detail}")
    for state, entries in groups.items():
        if len(entries) < 2:
            continue          # a lone branch is already its own chunk
        out.append(Page(
            url=page.url, title=page.title,
            markdown=f"# Locations in {state}\n"
                     f"{business or page.title} in {state} "
                     f"({len(entries)} branches)\n"
                     + "\n".join(entries)))

    rest = [ln for j, ln in enumerate(body) if j not in used]
    if rest:
        out.append(Page(url=page.url, title=page.title,
                        markdown="# " + page.title + "\n" + "\n".join(rest)))
    return out


def _strip_repeated_blocks(pages: list[Page], base_url: str) -> list[Page]:
    """Drop repeated blocks where they sit in a page's header/footer chrome.

    Floating chat widgets, cookie bars and "all our branches" footers are
    real text, so both trafilatura and the DOM fallback keep them — and
    then every chunk on the site carries the same 60 words, diluting the
    embedding and burning context. We keep the first occurrence, so the
    information is still in the index once, and drop the rest.

    Both the leading and trailing runs go, from every page including the
    first — keeping the "first" copy inline just meant 69 pages each held
    the same twelve branch addresses. Instead the removed blocks are
    filed once on a dedicated Contact & Locations page, so the addresses
    stay answerable without being duplicated across the whole index.

    A repeated block in the MIDDLE of a page is that page's own content
    and is left alone.

    Heading lines are never dropped: the chunker keys section boundaries
    off them, and removing one silently merges two sections together.
    """
    if len(pages) < MIN_PAGES_FOR_DEDUPE:
        return pages

    counts: Counter = Counter()
    for page in pages:
        counts.update({ln.strip() for ln in page.markdown.splitlines() if ln.strip()})

    limit = len(pages) * BOILERPLATE_RATIO
    boiler = {
        ln for ln, n in counts.items()
        if n > limit and not ln.lstrip().startswith("#")
    }
    if not boiler:
        return pages

    collected: list[str] = []
    seen: set[str] = set()
    out: list[Page] = []
    for page in pages:
        lines = page.markdown.splitlines()
        head, tail = _chrome_span(lines, boiler)
        keep: list[str] = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped in boiler and (i < head or i >= tail):
                if stripped not in seen:
                    seen.add(stripped)
                    collected.append(stripped)
                continue
            keep.append(line)
        out.append(Page(page.url, page.title, "\n".join(keep).strip()))

    if collected:
        out.append(_chrome_page(pages, base_url, collected))

    dropped = sum(p.words for p in pages) - sum(p.words for p in out)
    print(f"  boilerplate: {len(boiler)} repeated blocks, {dropped} words removed, "
          f"{len(collected)} filed on {out[-1].url}", flush=True)
    return out


def crawl(base_url: str, max_pages: int = 60, timeout: float = 20.0) -> list[Page]:
    base_url = base_url if base_url.startswith("http") else f"https://{base_url}"
    host = urlparse(base_url).netloc.replace("www.", "")
    pages: list[Page] = []
    seen: set[str] = set()

    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": UA},
    ) as client:
        queue = deque()
        sitemap = _sitemap_urls(base_url, client, host)
        if sitemap:
            print(f"  sitemap: {len(sitemap)} urls", flush=True)
            queue.extend(sitemap[: max_pages * 2])
        queue.appendleft(_normalise(base_url))

        throttle = _Throttle(HOST_DELAY)

        def fetch(url: str):
            throttle.wait()
            try:
                return url, client.get(url), None
            except Exception as exc:
                return url, None, exc

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            while queue and len(pages) < max_pages:
                batch: list[str] = []
                while queue and len(batch) < CONCURRENCY:
                    url = _normalise(queue.popleft())
                    if url not in seen:
                        seen.add(url)
                        batch.append(url)
                if not batch:
                    continue

                # Fetching is what costs wall time; extraction stays on this
                # thread so lxml and trafilatura are never touched concurrently.
                for url, r, err in pool.map(fetch, batch):
                    if err is not None:
                        print(f"  skip {url}: {err}", flush=True)
                        continue
                    if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                        continue

                    page = _extract(url, r.content)
                    if page and len(pages) < max_pages:
                        pages.append(page)
                        print(f"  [{len(pages):>3}] {page.words:>5}w  {url}", flush=True)

                    if not sitemap:
                        for link in _links(url, r.content, host):
                            if link not in seen:
                                queue.append(link)

    return _strip_repeated_blocks(pages, base_url)
