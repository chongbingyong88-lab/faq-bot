# FAQ bot — ingestion core

Paste a business URL, get a bot that answers questions from that business's
own website. Channel-agnostic: WhatsApp is a thin adapter over `query.py`.

## Setup

```bash
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -r requirements.txt
copy .env.example .env                              # add your GEMINI_API_KEY
docker compose up -d                                # postgres + pgvector on :5433
```

`GEMINI_API_KEY` is the only key the core needs — it serves both embeddings
and answers. `GROQ_API_KEY` is optional and only used by the `LLM_FALLBACK`
chain. No model weights are downloaded and torch is not a dependency; the
process is ~100MB and every embedding is a network call.

That last point has an operational cost worth knowing before you rely on it:
embeddings and answers draw on **separate** Gemini free-tier daily quotas,
the quota day rolls over at **midnight Pacific**, and failed retries are
billed against it like any other request. Exhausting it takes the bot down
until the reset, so `ingest/llm.py` keeps a cooldown file and falls back
across providers rather than hammering a dead model.

## Use

```bash
python -m ingest.run https://someclinic.com.my --site clinic
python query.py --site clinic
python -m ingest.run --list
uvicorn app:app --port 8000                         # webhook + /ask + dashboard
python evals/run.py --site clinic                   # score it
```

Re-running ingest for a site replaces it, so iterating on chunking is safe.

## How it works

| Step | File | Note |
|---|---|---|
| Crawl | `ingest/crawl.py` | sitemap.xml first, BFS fallback, trafilatura strips nav/footer; splits contact pages per branch |
| Chunk | `ingest/chunk.py` | heading-aware, packs short sections, windows long ones |
| Embed | `ingest/embed.py` | `gemini-embedding-001` at 1536 dims, multilingual (EN/MS/ZH in one vector space) |
| Store | `ingest/store.py` | pgvector, HNSW cosine index, one table sliced by site |
| Answer | `query.py` | retrieve 20 → keep top 5 by cosine → Gemini Flash-Lite → JSON with refusal flag |
| Serve | `app.py` | WhatsApp webhook (HMAC verify, dedupe on message ID, debounce), `/ask`, `/health` |

1536 dimensions is not a tuning choice: pgvector's HNSW index refuses more
than 2000, and Gemini defaults to 3072. Reduced-dimension embeddings come
back unnormalised, so `embed.py` normalises them itself.

Three design decisions worth keeping:

**Headings travel with their text.** "RM150" alone is unretrievable.
"Scaling & Polishing > Price: RM150" answers the question.

**Short sections are packed, not dropped.** Price lists and FAQ pages are
full of 10-word sections and those are the highest-value content on the
site. A naive minimum-length filter deletes exactly the answers customers
ask for.

**There is no reranker.** A bge-reranker cross-encoder cost 43s of the 45s
per reply and was reordering rather than rescuing — vector search alone
already put the right chunk in the top 20 on 97% of eval rows. `MIN_SCORE`
is therefore a cosine floor on a 0..1 scale, not a reranker logit, and it
still defaults to 0.0 pending calibration against the eval.

## Prompt guarantees

`query.py` forces the model to return `answerable: false` rather than guess,
and forbids inventing prices, hours, availability or policies. Two guards
run after the model, on its output rather than its intentions:

- prices and times in the reply are checked against the retrieved context,
  so a number the site never published cannot reach a customer; masked
  prices (`RM5xxx`) are treated as deliberately unpublished
- a reply that promises to "get back to you" is forced to `answerable:
  false`, because promising a follow-up without escalating to a human is
  worse than refusing

It also enforces one short message with no markdown — from 1 Oct 2026 Meta
bills per reply, so a bot that splits an answer into four bubbles costs 4x.

## Evaluating

`evals/<site>.yaml` holds the question set; `evals/run.py` scores it with an
LLM judge pinned via `JUDGE_MODEL` to a **different** provider than the bot,
so a model experiment is not graded on its own yardstick. Headline numbers
are accuracy on answerable questions and the false-answer rate; the second
matters more.

Two cautions, both learned the hard way and documented in `TUNING.md`:
repeated identical runs disagree on about **6%** of verdicts, so differences
smaller than that are noise, not progress; and a run with errored rows
scores a smaller denominator, which the report now says out loud rather than
quietly reporting a flattering number.

`TUNING.md` is the running log of what was tried, what moved the numbers and
what turned out to be measurement error. It is the most useful file here.

## Next

1. Calibrate the answer/defer threshold. Which way it errs depends on the
   site: on a content-rich one (466 chunks) the bot refuses 3-4 questions it
   could have answered and never answers one it shouldn't; on a thin one (87
   chunks) it answers 7 of 20 questions the site cannot support. Outright
   invented facts are down to 2% — but the model routes around the fact
   guards into unsupported *reassurance* ("most of our clinics are open
   9-6", "we work with selected corporate panels"), which no regex catches.
2. Majority-of-3 judging on boundary rows, to get under the 6% noise floor.
3. Survive a laptop sleep: Docker's socket handling takes the database down
   on every resume (see `TUNING.md`), which blocks any claim of unattended
   operation.
