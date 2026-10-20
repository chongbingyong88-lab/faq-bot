# Stage prompts 2–7

One section per stage. Tell Claude Code: **"Read the Stage N section of
STAGE_PROMPTS.md and follow it."**

Stages 2 and 5 are done. Stage 3 was a scaffold and is now filled in from
real numbers — including two levers it originally recommended that have
since been measured and ruled out. Stage 4 is built but its gate is unmet,
and its old deployment advice described a stack that no longer exists; both
are corrected below.

Stages 6 and 7 are still scaffolds. Their real content is whatever the
earlier stages find, so fill in the bracketed parts before using them. A
prompt that pretends to know the fixes before you have the numbers is
fiction — which is exactly how Stage 3 came to recommend a threshold that
does not work.

---

## Stage 2 — Eval harness

```
Read PLAN.md. We are on Stage 2 only. Stage 1 is done: sites are ingested
and I have read the chunks.

## Goal

A repeatable score for how good this bot actually is. Two numbers:
  1. Accuracy on answerable questions
  2. False-answer rate — how often it invented something instead of escalating

Number 2 matters more. A business owner forgives "let me check with my
colleague". They do not forgive a quoted price that doesn't exist.

## Build the eval set

50 questions per business, in evals/<site>.yaml:

  - question: "berapa harga scaling?"
    answerable: true
    expect: ["150"]          # facts that MUST appear in a correct reply
    note: "price is on the services page"

Rules for writing them — these matter:
- Write questions from the RENDERED PAGE as a customer would ask, not from
  the stored chunks. If questions are generated from the same chunks we
  retrieve, the eval is circular and always passes.
- Phrase them like real customers: lowercase, typos, no punctuation,
  "hi do u all open sunday ah"
- At least 10 in Malay, 5 in Chinese, 5 mixed/rojak
- Exactly 10 with answerable: false — plausible questions the site genuinely
  does not answer (do you do payment plans, is parking free, do you accept
  my insurance). These are the most important rows in the file.

Generate a first draft yourself, then STOP and let me review before scoring
anything. I will correct them. Do not proceed until I say the set is good.

## Build the scorer

evals/run.py --site <site>, which for each question calls answer() and checks:
- answerable: true  -> did the reply contain every string in `expect`?
- answerable: false -> did the model return answerable: false?

For fuzzy cases use a separate LLM-as-judge call with a strict rubric. The
judge must see ONLY the question, the expected facts and the reply — never
the retrieved context, or it will rationalise a wrong answer as correct.

Output: the two headline numbers, then a table of every failure with the
question, what came back, and the top rerank score. The failure table is
what I actually use in Stage 3.

Save each run to evals/results/<site>-<timestamp>.json so we can compare.

## Out of scope

Do not fix any failures. Do not tune chunking, retrieval or prompts. We are
measuring, not improving. Note what you'd change and stop.

## Done

One command prints both numbers and a failure table for each site.
```

---

## Stage 3 — Tune (filled in from the 10 Sep 2026 re-baseline)

```
Read PLAN.md, and the "Re-baselined on the fixed build" section of TUNING.md.
Stage 3. Current numbers — all on gemini-3.1-flash-lite, judge pinned to
groq:qwen/qwen3.8-27b:

  sepadental   36/40 answerable (90%)   refusal 10/10   0 invented
  tiewdental   25/30 answerable (83%)   refusal 17/20   0 invented
  khengdental  35/42 answerable (83%)   refusal  8/20   1 invented in 62

The gate is >=90% answerable AND zero invented. Only sepadental passes.

## The failure that matters

khengdental answers 12 of 20 questions its site cannot support, 6 of them
critical — not with invented numbers, since the price guard stopped those,
but with unsupported reassurance:

  "do you accept AIA insurance"            (the site names no insurer)
    -> "We work with selected corporate panels and are happy to assist
        with claim documents."

  "ada khidmat kecemasan waktu malam tak"  (no night service is published)
    -> "Kebanyakan klinik kami beroperasi dari 9 pagi hingga 6 petang..."

No pattern-matching rule catches an adjective. The other two sites score 0
and 3 on the same failure, so this is a property of thin, near-miss content
as much as of the prompt.

The OPPOSITE failure dominates sepadental: deferring on services the site
plainly does list. Both directions are the same knob — do not fix one and
report it as progress without re-checking the other.

## Two levers already RULED OUT — do not spend a day rediscovering these

1. MIN_SCORE. Measured on khengdental, 10 Sep 2026. Correct answers score
   0.620-0.730, false ones 0.620-0.690 — the bands overlap almost exactly:

     floor 0.64  ->  keeps 30/35 correct, blocks 3/7 false
     floor 0.66  ->  keeps 27/35 correct, blocks 5/7 false
     floor 0.70  ->  keeps  9/35 correct, blocks 7/7 false

   Every threshold costs more good answers than bad ones it blocks. A
   similarity score can say the passage is ABOUT the topic; it cannot say
   the passage CONTAINS the fact. Only reading it can.

2. Hybrid BM25 search. This was gated on failures being exact-match misses.
   They are not. Retrieval on the current build is 42/42 khengdental, 30/30
   tiewdental, 38/40 sepadental. Both sepadental misses are genuine and
   neither is an exact-match problem: one answered correctly from adjacent
   content, the other is the registered company name, which appears once in
   a page footer and ranks 15th. The facts are reaching the model, which
   then talks past them.

## The lever that is left

Prompt wording in query.py SYSTEM. Rule 2 already says "if the context does
not contain the answer, set answerable to false" — it is losing to context
that is merely adjacent. Worth trying, ONE at a time:

  - require the model to quote the span it is answering from, and refuse
    when it cannot produce one
  - name the failure mode explicitly: a reply describing what the business
    generally does, with no specific supporting line in the context, is a
    refusal
  - separate "the context is about this topic" from "the context answers
    this question" in the instruction itself

## Method — corrected for the noise floor

Two identical runs of this eval disagree on about 6% of verdicts, roughly a
third of that from the judge changing its mind on byte-identical text. So:

- A change smaller than 6% is NOT evidence. Do not record it as progress.
- Iterate with `--rerun-fails <results.json>`, which scores only the rows
  that failed last time (~34 calls instead of 162). Full runs confirm a win;
  they are too expensive and too noisy to hunt one.
- Confirm any apparent win with a PAIRED full run — same build, twice — and
  report both numbers, not the better one.
- Append every attempt to TUNING.md, including the ones that did nothing.
  The reverted experiments are the most useful rows in that file.

## Budget

A full re-baseline is ~162 scored rows plus a judge call each, and it
consumed most of a day's free quota. The Gemini quota day rolls over at
midnight PACIFIC (15:00 MYT), and retries are billed against it. Check
data/llm_cooldown.json before starting, and do not begin a full run late in
a spent day — a run that dies mid-way scores a smaller denominator and is
not comparable to anything.

## Done

>=90% on answerable questions and ZERO invented facts, on all three sites,
confirmed by a paired run. TUNING.md shows the path taken, including what
failed.

Do not proceed to Stage 4 on a bad number. That number is the product.
```

---

## Stage 4 — FastAPI service (built; gate NOT met)

```
Read PLAN.md. Stage 4. The service already exists in app.py:

  POST /ask, GET /health, /dashboard, /escalations, and the WhatsApp
  webhook. Structured JSON logging of every request is in place.

What is NOT done is the gate. Measured 10 Sep 2026: median reply 4.1s,
range 2.2-6.3s over seven live replies, every one served by the primary
model — but on a laptop, behind ngrok, deployed nowhere.

## The warnings that used to be in this section are obsolete

It used to say: load the embedder and reranker once at startup, budget ~4GB
RAM for BGE-m3, and "do not silently swap the embedding model — it would
invalidate every Stage 3 number."

The reranker is gone; it cost 43s of a 45s reply. Embeddings are a hosted
API call (gemini-embedding-001 at 1536 dims), so there are no weights to
load, no 4GB floor, and no thread-safety problem with local inference. The
process is ~100MB.

The swap that warning was about DID happen, and the warning was right: every
number predating it had to be re-measured. Keep the rule for next time.

## What actually bites now

1. The daily free quota, not RAM. Embeddings and answers draw on SEPARATE
   per-model daily caps, retries are billed against them, and the day rolls
   over at midnight Pacific. LLM_FALLBACK across a different provider AND a
   different model family is what keeps the bot up — not a bigger instance.

2. Latency is a provider property now, so the failover needs a time budget,
   not just an error check: LLM_DEADLINE_S and LLM_ATTEMPT_TIMEOUT_S. Note
   that Gemini enforces the per-attempt timeout server-side and returns 504
   DEADLINE_EXCEEDED rather than dropping the socket, and its floor is 10s,
   so a lower value is silently ignored. Keep DEADLINE well above ATTEMPT or
   the chain runs out of budget before it can fall back.

3. The database has to outlive a sleeping laptop. On 10 Sep the machine
   slept and Docker's own socket files blocked its restart for nine hours.
   Managed Postgres removes this failure entirely, and it is the single
   strongest reason to deploy.

## Deployment reality check

Target a small instance with managed Postgres + pgvector. The old 4GB floor
no longer applies, so the cheap tiers are viable. Before deploying, confirm:

  - pgvector is available, and the HNSW index dimension limit (2000) is not
    exceeded — we use 1536 deliberately, because Gemini defaults to 3072
  - outbound HTTPS to the model providers is allowed
  - storage is a managed DB or a persistent volume, NOT container-local
  - back up before cutting over; data/backups holds a verified pg_dump

## Done

curl from my phone's mobile data returns a correct answer in under 3
seconds, against the deployed instance, with my laptop closed.
```

---

## Stage 5 — WhatsApp adapter

```
Read PLAN.md. Stage 5 only. The /ask service is deployed and working.

## Meta setup (I do this part, you guide me)

Walk me through: creating the Meta app, adding WhatsApp, getting the free
test number, and registering my phone as a recipient.

Important: the test number allows unlimited messages to up to 5 verified
recipients, and once a number is added it CANNOT be removed. Warn me before
I add any number.

## Build

- GET /webhook/whatsapp  -> verification challenge (hub.challenge)
- POST /webhook/whatsapp -> messages
- Verify X-Hub-Signature-256 as an HMAC over the RAW body. Reject if it
  doesn't match. Getting the raw body in FastAPI is fiddly — do not use the
  parsed dict for signature checking.
- Return 200 IMMEDIATELY, then process in a background task. Meta retries
  aggressively on slow responses and you'll get duplicate replies.
- Dedupe on message id: persist seen ids, ignore repeats.
- Debounce: people send five short bubbles instead of one message. Wait ~2s
  for more from the same sender, concatenate, then answer once. This also
  saves money — from 1 Oct 2026 Meta bills per reply.
- Send exactly ONE message back. No splitting.

Text messages only in this stage. Media is Stage 6 — if a non-text message
arrives, reply asking for text and log it.

Use ngrok for local development.

## Done

I message the test number from my own phone and get a sane reply. Duplicates
and rapid-fire bubbles both handled correctly.
```

---

## Stage 6 — Survive real humans (scaffold)

```
Read PLAN.md. Stage 6. The bot works for clean text. Now make it survive
real users.

Log of what broke during the week of testing:
  <paste actual failures here before starting>

Likely work, in priority order — confirm against the log above:
- Voice notes: download media via the Meta media endpoint, transcribe,
  answer as normal. Very common in Malaysia.
- Images: someone photographing a price list or a rash. Decide the policy —
  probably escalate to human rather than attempt vision.
- Human handoff: a clear trigger, an escalation log, and a notification I
  actually see. Currently answerable:false just replies politely and the
  message is lost.
- Per-user rate limiting.
- A safe fallback reply on any unhandled exception. The bot must never go
  silent mid-conversation.

Prioritise from the real log, not from this list.

## Done

A full week with no reply I'd be embarrassed to show a business owner.
```

---

## Stage 7 — Demo polish (scaffold)

```
Read PLAN.md. Stage 7. Everything works. Make it demoable.

## The one that matters

URL in, working bot out, in under 10 minutes, so I can prepare a prospect's
own bot before meeting them. Make that a single command with clear progress
output. Everything else here is secondary.

## Also

- A small dashboard: questions asked, % escalated, response times, recent
  conversations. Business owners understand this; they don't understand
  rerank scores.
- Keep the web chat UI working as a fallback for anyone not in my 5
  WhatsApp test numbers, and for when venue wifi fails.

## Out of scope

No pricing pages, no billing, no multi-tenant auth. Not yet.

## Done

I can go from "here's their URL" to a working bot in one sitting.
```
