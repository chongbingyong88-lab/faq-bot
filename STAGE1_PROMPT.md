# Stage 1 kickoff prompt

Paste the block below into Claude Code, or just say:
**"Read STAGE1_PROMPT.md and follow it."**

Replace `<ANCHOR_URL>` and `<LOCAL_URL>` before starting.

---

```
Read PLAN.md and README.md first. We are on Stage 1 and only Stage 1.

## Context

This is a RAG pipeline that ingests a business website and answers customer
questions from it. It will later sit behind WhatsApp, but not yet.

The code is written but has NEVER run against a real website — only against
synthetic markdown I made up. Assume it is broken until proven otherwise.
Every step in this pipeline fails silently: the crawler returns 200 OK on a
JS-rendered page and extracts nothing, the chunker happily processes garbage,
embedding succeeds on garbage. Nothing throws. So we validate by looking at
actual output, not by checking for errors.

Environment: Windows, Python, Docker Desktop available.

## Scope — do not exceed this

IN scope:
- Getting the environment running
- Ingesting real sites and inspecting what comes out
- Fixing crawler/chunker bugs that real sites expose
- A script to dump chunks so I can read them

OUT of scope — do not build these even if you think they're needed:
- The eval harness (Stage 2)
- Prompt tuning or retrieval tuning (Stage 3)
- FastAPI service (Stage 4)
- Any WhatsApp code (Stage 5)
- Hybrid search / BM25 — noted as a possible Stage 3 change, not now

If you think something out of scope is genuinely blocking, say so and wait
for me. Do not start it.

## Step 1 — Environment

Set up the venv, install requirements.txt, start Postgres via docker compose,
create .env from .env.example. Verify the DB is reachable on port 5433 and
that pgvector loads.

Note: the first embedding run downloads ~2.3GB of BGE-m3 weights and will
look frozen for several minutes. That is expected, not a hang.

## Step 2 — Anchor site

Ingest <ANCHOR_URL> first. This is a large, content-heavy site chosen
deliberately: if the pipeline can't handle it, the bug is in our code, not
in a thin website.

Show me, before touching any code:
- pages crawled, and words extracted per page
- total chunks, and the chunks-per-page ratio
- the distribution of chunk word counts (min / median / max)

Healthy is roughly 5–15 chunks per page. One chunk per page means the
packing logic in chunk.py is too aggressive. Fifty means it's fragmenting.

## Step 3 — Chunk dump script

Write `dump_chunks.py` that prints stored chunks for a site in readable
form — url, heading path, then the text — with `--site` and `--limit`
flags and an option to write to a file. Keep it under 60 lines. This is a
tool for me to read output with, not a feature.

Then show me 30 chunks sampled across different pages. I will read them
myself and judge. The question I'm answering is NOT "is the answer good" —
it's "could a human answer a customer question from this text alone?"

## Step 4 — Local site

Now ingest <LOCAL_URL>, a real small business site. Report the same numbers.

If the crawler returns zero pages: that is very likely a JS-rendered site
(Wix, Squarespace, some Shopify), not a bug. Confirm by checking whether
raw HTML contains the visible text. If it's JS-rendered, tell me and stop —
I'll pick a different candidate. Do NOT add Playwright or a headless
browser without asking.

## Step 5 — Retrieval sanity check

Run 5 questions through query.py against each site, including one in Malay
and one in Chinese. For each, show me the RETRIEVED CHUNKS and rerank
scores, not just the final answer. I'm inspecting retrieval, not answers.

## Known failure modes to watch for

Flag these if you see them, propose a fix, but change one thing at a time
and re-run so we can see the effect:

- Price tables flattened into unreadable runs of numbers. On a clinic or
  florist site this is the highest-value content, so check it first.
- Nav/footer boilerplate repeated in every chunk, diluting embeddings.
- A sitemap full of hundreds of blog posts drowning the 6 pages that matter.
- Chunks that are only a heading with no body.
- Malay/Chinese pages chunking fine but retrieving badly for English queries.

## Working style

- Explain what you found before changing anything.
- One change at a time, re-run, show the diff in output.
- Don't refactor working code for tidiness.
- If a fix requires a design decision, ask me — don't pick for me.

## Definition of done

- Both sites ingested with sensible page and chunk counts
- dump_chunks.py exists and works
- I have read ~30 chunks and confirmed they could answer real questions
- A short summary of what broke on real sites and what you changed

Stop there. Do not start Stage 2.
```
