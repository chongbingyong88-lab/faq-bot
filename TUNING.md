# Tuning log

Stage 3 rule: change ONE thing, re-run the full eval, record both numbers.
If a change doesn't move them, revert it.

Accuracy = % of answerable rows correct. Fabrications = replies containing a
price or time not present in the retrieved context (the number that matters).

| # | date | change | sepa acc | tiew acc | fabrications | keep? |
|---|---|---|---|---|---|---|
| 0 | 07 Sep | baseline: BGE-m3 + reranker, Haiku-era prompt | 68% | 73% | **15** | — |
| 1 | 07 Sep | price rules in prompt (market rates / masked / discount) | 68% | 63% | **0** | ✅ kept |
| 2 | 07 Sep | language detect + per-request instruction; BE SPECIFIC; confirm-service | 78% | 80% | 0 | ✅ kept |
| 3 | 07 Sep | masked-price guard `x{2,}` → `x+`; split fabrication vs escalation-flag metric | 70% | 77% | 0 | ✅ kept |
| 4 | 07 Sep | fact-verification guard (any price/time not in context → block) | not measured | not measured | 0 | ✅ kept |
| 5 | 07 Sep | conversation memory ON; rule 4 tightened (no other treatment's price) | not measured | not measured | not measured | ✅ kept |
| 6 | 07 Sep | **BASELINE** of current build on `gemini-3.1-flash-lite` | **78%** | **73%** | **0** | — |
| 7 | 07 Sep | model → `gemini-3.5-flash` (experiment 1) | **INCONCLUSIVE** | — | — | ❌ reverted |

Run 7 aborted after 14 of 50 rows: `gemini-3.5-flash` free-tier quota is spent
by roughly 20 calls/day, like `gemini-3.8-flash`. The partial run showed 43%,
but that is 14 rows against a 40-row baseline and is **not a result** — do not
cite it. **Experiment 1 needs paid Gemini and is blocked, not answered.**

Practical consequence: on the free tier you are limited to flash-*lite* models
(`gemini-3.1-flash-lite`, `gemini-3-flash-preview` at ~500/day). Testing
whether a stronger model closes the "fact omitted" gap costs money — probably
the single highest-value thing to spend a small budget on.

| # | date | change | sepa acc | tiew acc | fabrications | keep? |
|---|---|---|---|---|---|---|
| 8 | 07 Sep | KEEP 5 → 3 (experiment 2) | 78% (=) | **77%** (+4) | 0 | ✅ kept |

Run 8: no change on sepadental, +4 points on tiewdental — inside the ±8 noise
band, so treat as "no worse, possibly better". Kept anyway because it is
strictly cheaper: 3 chunks instead of 5 per request means a smaller prompt,
lower cost and lower latency for the same accuracy. Escalation-flag errors also
went to 0/10 and 0/20 on both sites.

Watch: retrieval failures rose 2% → 5% (sepa) and 0% → 7% (tiew). With KEEP=3
the correct chunk falls outside the window more often. It has not hurt accuracy
yet, but KEEP=2 would likely start to.

| # | date | change | sepa acc | tiew acc | fabrications | keep? |
|---|---|---|---|---|---|---|
| 9 | 08 Sep | worked GOOD/BAD example in prompt (experiment 3) | 75% (−3) | 77% (=) | 0 | ❌ **reverted** |

Run 9: no improvement on either site, and it added ~200 tokens to every
request. Reverted per the change-one-thing rule. The fake example values
(`Exampletown`, `Jalan Contoh`) leaked into **0** replies, so the containment
worked — worth remembering if few-shot is tried again with real-looking data.

Conclusion so far: **prompt wording has stopped paying.** Runs 1–2 bought a lot,
runs 3 and 9 bought nothing. Both remaining failure classes (fact omitted 63%,
hedged 37%) look like model capability, not instruction — which is exactly what
experiment 1 would test if the quota allowed it.

Recommended next: spend a few dollars on Gemini credit and run experiment 1
(`gemini-3.5-flash`) properly. Experiment 4 (answer-shape rule) is still free
but, after two failed prompt experiments, expected value is low.

## Free-tier quota, measured

| model class | requests/day | enough for a 50-row eval (~85 calls)? |
|---|---|---|
| `gemini-*-flash-lite` | **500** | yes, ~5 runs/day |
| `gemini-*-flash` (non-lite) | **20** | no — dies after ~14 rows |

Counters are per model per day and reset around 00:00 US Pacific (~3pm MYT).
Three eval runs in one day drains a lite model. Budget roughly **one tuning
experiment per lite model per day**, or pay.

This is the binding constraint on tuning, not ideas. Experiment 1 needs paid
credit; everything else is rationed.

## Bug found while switching models (fixed, run 10)

`gemini-3-flash-preview` is a thinking model: at `LLM_MAX_TOKENS=600` it spent
the budget reasoning and returned **truncated JSON**, which failed to parse —
and the old fallback put the raw model output straight into `reply`, so a
customer would have received `{"answerable": true, "reply": "The cost for...`
verbatim. Now a parse failure returns a safe apology and records the raw text
under `parse_failed` / `raw` for debugging. `LLM_MAX_TOKENS` raised to 1500.

Worth knowing: this had nothing to do with tuning. It surfaced only because a
model switch forced a different failure mode.

Runs 1–3 were on `gemini-3.5-flash-lite`. Runs 4–5 changed behaviour but were
verified by unit tests and `evals/conversations.py` (5/5), not a full eval —
quota ran out. **The current build has no measured accuracy number.**

Note on noise: sepadental went 68 → 78 → 70 across runs 1–3 with changes that
should only have helped. With 40 answerable rows a 3-row swing is ±8 points, so
treat any single-run difference under ~10 points as noise. Average two runs
before believing a result.

## Where the remaining failures are

From run 3 (19 accuracy failures across both sites):

| share | cause | note |
|---|---|---|
| 63% | fact omitted | right chunk retrieved, reply left out the address/time |
| 37% | hedged | didn't plainly confirm a service the context lists |
| 0% | wrong language | fixed in run 2 |
| 0% | retrieval miss | retrieval is not the problem |

Similarity on failed rows: min 0.59, median 0.71, max 0.77 — i.e. as high as
passing rows.

## Do NOT bother with

- **MIN_SCORE.** Stage 3 calls it knob #1, but failures aren't low-similarity.
  Raising the floor would discard correct context and fix nothing. Left at 0.0.
- **Chunk sizes.** Requires a re-ingest and retrieval is already at 98–100%.
  Last resort, not first.

## Planned experiments, highest expected value first

Each is one change, then a full eval on both sites (~12 min).

1. **Bigger model** — `gemini-3.5-flash` instead of `gemini-3.1-flash-lite`.
   Both remaining failure classes are "had the information, didn't state it
   precisely", which is capability, not prompting. One line in `.env`.
2. **KEEP 5 → 3.** Five chunks may dilute attention; the answer is usually in
   the top one or two. Cheap, and reduces cost per reply.
3. **A worked example in the prompt.** Show one question and one ideal reply
   that quotes a full address verbatim. Few-shot beats instruction for
   "be specific" style failures.
4. **Separate the ask.** For where/when questions, instruct the model to state
   the detail first and add context after — omissions cluster in replies that
   open with a summary sentence.

Stop when accuracy ≥ 90% with fabrications at 0, or when two consecutive
changes fail to move either number.

---

## Run 11 (08 Sep) — the accuracy ceiling was a retrieval bug, not the model

**Everything above this line about "fact omitted, 63%" was wrong.** The
failures were never generation failures. Correcting the record:

### What actually happened

`retrieval_ok` compared the retrieved chunk's **URL** against
`retrieval_target`. Every SEPA branch address lived in one chunk filed under
`/contact-us`, so any question about any branch matched that URL and scored
"retrieved: True" — while the chunk the model actually saw held a different
branch, or no address at all.

Checked directly against chunk text instead of URLs:

| required fact in the retrieved context | before | after |
|---|---|---|
| sepadental, the 12 rows that kept failing | **0/12** | **12/12** |
| tiewdental | 3/5 | 4/5 |

Zero. The model was refusing to state addresses it had never been given —
which is the correct behaviour, and exactly what the safety rules ask for.

### Why the addresses vanished

The Stage 1 boilerplate consolidation. Stripping the repeated footer removed
each branch's address from its own page (the TTDI page has 7 chunks and not
one contains an address), and filed all twelve in a single 1195-char chunk.
That chunk's embedding averages twelve localities, so it ranked **7th, 10th,
11th, or nowhere** for a single-branch question. It was never in the top 3.

This is the failure mode flagged twice during Stage 1 review. The
"6/6 addresses kept" check missed it because it tested whether the addresses
existed in the index, not whether any single-branch query could reach them.

### The fix

`split_contact_page()` in `ingest/crawl.py` — one page per address, with the
branch label and phone paired to it by position:

    # SEPA Dental - TTDI, KL
    SEPA Dental - TTDI, KL
    011-5453 6787
    B-1-11, TTDI Plaza, Jalan Wan Kadir 3, ... 60000 Kuala Lumpur

Applied at chunk time, not crawl time, so a cached crawl gets it without a
refetch: re-ingest was 4.6s and 13 new embeddings. Rank for the failing
questions went 7–11–absent to **1–2**. Phone-per-branch came along free.

Sites with no consolidated contact page (tiewdental) are unaffected — the
splitter returns the page untouched when it finds fewer than 2 addresses.

| # | date | change | required facts reaching model | keep? |
|---|---|---|---|---|
| 11 | 08 Sep | split contact page per branch | 0/12 -> 10/12 | yes |
| 12 | 08 Sep | KEEP 3 -> 5 (reverts run 8) | 10/12 -> **12/12** | yes |

Run 8 lowered KEEP to 3 and measured "no worse". It was no worse *because the
address chunks did not exist yet* — KEEP could not matter. Now it does: the
last two facts sit at ranks 4 and 5. KEEP=8 adds nothing over 5.

Direct check on 4 previously-failing address questions, `gemini-3.6-flash`:
**4/4 correct**, full street address quoted verbatim.

### Not yet measured

A full LLM-scored eval on both sites. Free-tier quota was spent diagnosing
this. The retrieval numbers above need no LLM and are solid; the accuracy
number is pending a run after the daily reset.

### Method fixes made at the same time

- **`JUDGE_MODEL`** (`evals/run.py`). The judge called the same `generate()`
  as the bot, so `GEMINI_MODEL` moved the grader *and* the thing being
  graded. Runs 7 and 9 were scored on a different yardstick than their own
  baselines. The judge is now pinned independently, and draws on a separate
  per-model daily quota.
- **`retrieval_ok` checks context text**, not URLs. This is the bug that hid
  the whole problem — a metric that could not fail.
- **`--rerun-fails RESULTS.JSON`** re-scores only the rows that failed in a
  previous run, so a 20-request/day model can still say something useful.
- `answer()` returns `context` so the scorer can see what the model saw.

## Free-tier quota, corrected

Aliases and `-preview` suffixes resolve to the **same** bucket as the model
they point at, so they are not extra capacity:

| id | real bucket | limit/day |
|---|---|---|
| `gemini-flash-latest` | `gemini-3.8-flash` | 20 |
| `gemini-3.1-flash-lite-preview` | `gemini-3.1-flash-lite` | 500 |
| `gemini-flash-lite-latest` | `gemini-3.5-flash-lite` | 500 |

A one-token probe is not a quota test — several models answered a 5-token
"hi" and then 429'd on a real request. Probe with a realistic payload.

## Revised guidance

Experiment 1 (a bigger model) is no longer the top priority and paying for
Gemini credit is no longer the recommended next spend. `gemini-3.8-flash`
fixed only 2 of 9 failing rows, because a stronger model cannot state a fact
it was never given. Re-check the failure mix after the next full eval before
spending anything.

The general lesson: **verify a metric can fail before trusting what it says.**
Three tuning runs were spent on prompt wording for a problem that lived in
the chunker, because the retrieval metric was structurally incapable of
reporting a miss.

---

## Run 13 (08 Sep) — retrieval ceiling, measured without the LLM

`evals/retrieval.py` scores whether the strings each eval row requires are
in the context the model would be given. A row that fails here cannot pass
`evals/run.py` under any model or prompt, so it is the ceiling on accuracy.

| site | before the split | after split | after state grouping |
|---|---|---|---|
| sepadental | (not measurable) | 89% | **100%** (44/44) |
| tiewdental | — | 97% | 97% (28/29) |

sepadental now has **zero** facts ranked too low and **zero** missing from
the index. tiewdental's last gap is an artefact of the checker, not the
retriever — see below.

### A regression the ceiling caught immediately

Splitting per branch broke the questions the blob was good at: "do you have
any clinic in johor" wants four addresses and no single branch chunk holds
them — they ranked 6, 7, 7 and 12. Fixed by also emitting one page per state
(`_state_of` in `ingest/crawl.py`), so specific questions match the branch
chunk and regional ones match the group. 16 pages from the one contact page.

Worth noting the shape of this: the split fixed 12 rows and broke 1, and
without a cheap retrieval metric the net would have looked like a win and
the regression would have shipped.

## Two bugs in the new tool, found by using it

- **`required_any` was scored as all-of.** A row saying
  `required_any: [implant, 植]` was counted as two requirements, so a reply
  that correctly said "implant" still showed a miss on 植. Both sites' last
  "crawl bug" was this. `requirements()` now returns (label, alternatives)
  and any one alternative satisfies the group.
- **The sweep re-embedded everything six times.** `search` results do not
  depend on KEEP — only the slice handed to the model does. The ranking is
  now memoised per question and reused across KEEP values.

## Embeddings are NOT free

    embed_content_free_tier_requests, limit: 1000, model: gemini-embedding-1.0

A separate bucket from generation, and re-running the retrieval scorer
uncached exhausted it in one afternoon. Query vectors are now cached to
`data/vectors/queries-<model>-<dim>.npz`, so re-runs cost nothing.

Consequence while it is exhausted: the bot cannot retrieve **or** generate.
Embedding failure is the harder outage — it takes out `answer()` before the
model is ever reached.

## The promise nobody was keeping

`app.py` caught answer failures and replied "Let me pass you to a colleague"
— but set `result = {}`, so `escalated` was false and **no human was ever
notified**. Every customer during an outage got an apology that reached
nobody. The failure path now escalates, and the human's message says
`BOT IS DOWN: <error>` so an outage is distinguishable from "the site does
not say", which needs a completely different response.

This is only visible when the bot is broken, which is why three weeks of
green tests never showed it.

## Still not measured

Accuracy on either site, under the fixed retrieval. Blocked on generation
quota. Both ceilings say the headroom is there; whether the model uses it
is the open question.

Run this first after the reset:

    python evals/retrieval.py --site sepadental          # confirm 100%
    JUDGE_MODEL=gemini-3.1-flash-lite python evals/run.py --site sepadental
    JUDGE_MODEL=gemini-3.1-flash-lite python evals/run.py --site tiewdental

Judge pinned to a different model id than the bot, so the grader stays put
and the two draw on separate daily quotas.

---

## Run 14 (08 Sep) — first trustworthy accuracy number

Bot `gemini-3.5-flash-lite`, judge pinned to `gemini-3.1-flash-lite`,
KEEP=5, per-branch contact split, retrieval ceiling 100% on both sites.

| | baseline (run 8) | run 14 | delta |
|---|---|---|---|
| sepadental accuracy | 78% | **85%** (34/40) | **+7** |
| tiewdental accuracy | 77% | **80%** (24/30) | **+3** |
| sepadental fabrications | 0 | **1** | **-1** |
| tiewdental fabrications | 0 | **0** | = |
| retrieval failures | 2% / 7% | **2% / 0%** | better |

tiewdental scored all 50 rows this time; run 8 managed 37 before quota.

### The judge change did not move the numbers

This run pinned the judge to a different model than the baseline used, which
would normally make the comparison worthless. Re-judging every judge-only
failure with the baseline's own model (`evals/rejudge.py`, bot calls: zero):

    sepadental   0/6 overturned   34/40 -> 34/40
    tiewdental   1/6 overturned   24/30 -> 24/30  (the flip was an
                                   unanswerable row, so accuracy unchanged)

Two independent judges agree on 11 of 12. So the +7 and +3 are real, and the
judge is **not** a significant error source — despite several failures
looking, on a read-through, like judge nitpicks. They were not. Read the
replies before blaming the grader.

### Better retrieval created a new fabrication

sepadental's single fabrication is the Invisalign discount:

    Q  invisalign how much after discount
    A  The cost varies from patient to patient... Please note that the
       discount offered is RM2,000 OFF

RM2,000 is genuinely published — the site states the discount but never the
base price. Before run 11 that chunk was unreachable, so the row passed by
accident: the bot could not quote a number it had never been given. Fixing
retrieval fixed the two "is there a promotion" rows and broke this one.

This is the same failure class as the khengdental price tiers: **the number
is in the context and still wrong in context.** `unverified_facts` cannot
see it, because that guard only asks whether a price appears in the
retrieved text. Quoting "RM2,000 off" to "how much after discount" implies a
price that was never published.

Worth stating plainly: making retrieval better makes this class *more*
likely, not less. Every fact newly within reach is a fact that can be
deployed in the wrong place.

### Where the remaining failures are

sepadental 6 of 8 failures cluster on the RM99 scaling promotion — whether
it is still valid, and whether it applies at a named branch. The site
announces the promo without dates or branch scope, so the honest answer is
an escalation and the bot instead answers from the announcement.

tiewdental failures are opening hours per branch (the fact is retrievable —
`retrieval_ok: True` — and the bot escalates anyway) plus one reply that
answered an English question in Malay.

Retrieval is no longer the constraint on either site. It is now genuinely a
generation problem, which is what runs 1-9 wrongly believed.

### Stage 3 gate

    required   >=90% accuracy AND zero invented prices
    actual     85% / 80%, one fabrication on sepadental

Still not met, and now failing on the fabrication half too. But the gap is
5-10 points rather than the 12-13 it looked like, and the causes are
specific and named rather than "the model is not good enough".

---

## Runs 15-16 (08 Sep) — Stage 3 gate met on sepadental

| run | change | sepa acc | tiew acc | fabrications |
|---|---|---|---|---|
| 14 | baseline, retrieval fixed | 85% | 80% | 1 / 0 |
| 15 | partial answers + discount rule + final-price guard + English lang | **92%** | **83%** | 1 / 0 |
| 16 | scope the partial-answer rule away from prices | **95%** | 80% | **0 / 0** |

    sepadental   38/40 answerable, 0 fabrications   <-- >=90% and zero: GATE MET
    tiewdental   24/30 answerable, 0 fabrications

sepadental by language after run 16: en 91%, ms 93%, rojak 100%, zh 100%.

### Run 15: four changes, one diagnosis

The failures were not caution in general — the bot escalated the WHOLE message
when the CONTEXT answered PART of it. "Is the RM99 scaling available at Bukit
Jalil" is confirmable (advertised site-wide, no branch excluded); only the
branch detail is not. Rule 2 gained its counterweight: give the part you hold,
escalate only what is missing, and *the CONTEXT naming no end date and
excluding no branch is not a reason to refuse*.

Alongside it:

- **Rule 3c tightened.** It already covered discounts but only forbade
  *deriving* a price from one, not repeating it — and the model walked
  through that gap with "the discount offered is RM2,000 OFF".
- **`FINAL_PRICE_QUESTION` guard.** Deterministic: any RM figure in a reply to
  "how much after discount" / "selepas diskaun" / "折扣后" is blocked. Prompt
  rules on prices have leaked before; this one cannot.
- **A real language bug.** `detect_language` returned None for English, so
  English questions got **no language instruction at all** and the model
  drifted — "what time do you close on saturday" came back entirely in Malay.
  Now returns "English" at zero Malay markers, and still defers to the model at
  exactly one marker so rojak keeps mirroring the mix.

The language fix landed where predicted: sepadental rojak 67% -> 100%,
zh 88% -> 100%.

### Run 15 also caused a regression, and the bundling caught it

Fabrications stayed at 1 but changed identity, to something worse:

    Q  veneer price per tooth
    A  Dental veneers cost RM250 to RM2,500 per tooth

Retrieved from `porcelain-vs-composite-veneers`, `are-veneers-right-for-you` —
**blog articles**, not a price page. A market rate the clinic never charges:
exactly the class rule 3a was written for in run 1. "Answer what you CAN"
overrode it.

Attribution survived the bundle by construction: the deterministic guard can
only *reduce* fabrications, so an increase could only have come from the
prompt. Worth repeating as a technique — bundle changes whose error
directions differ, and a combined run still tells you which one moved.

Run 16 fixed it with one sentence: *an article, blog or treatment guide on
this business's own site is not a price list ... Answer more, quote no more.*
Accuracy went **up** 3 more points, so the scoping cost nothing.

### tiewdental 83% -> 80%

One row, inside noise for a 30-row denominator. The run was also degraded: 4
rows unscoreable on quota, 628s against a usual 300s. Do not read it as a
real decline; re-run before believing either number.

### What is left on sepadental (2 accuracy failures)

1. **"Sila hubungi kami"** — the new closing rule says never send the customer
   away, and it holds in English but not Malay. Same shape as the masked-price
   leak in run 3: a rule written in English degrades in the other languages.
   Rules that must survive translation belong in code, not the prompt.
2. **"is dr darren yap available"** — should confirm he practises at SEPA and
   escalate only the schedule. The partial-answer rule did not reach this case.
3. **"do you treat crooked teeth"** — says "clear aligners" where the site says
   "Invisalign". Needs a rule to use the CONTEXT's own service names.

### Quota spent

`gemini-3.5-flash-lite` drained (500/day) after four eval runs plus the
conversation suite. `gemini-3.1-flash-lite` (judge) still has room. Embeddings
comfortable thanks to the query cache.

---

## Run 17 (08 Sep) — a second provider: Groq

Bot `qwen/qwen3.8-27b` on Groq, judge pinned to `gemini:gemini-3.1-flash-lite`
so the grader did not move with the bot.

| sepadental | gemini-3.5-flash-lite (run 16) | qwen3.8-27b (run 17) |
|---|---|---|
| accuracy | 95% (38/40) | **98%** (39/40) |
| fabrications | 0 | 0 |
| en | 91% | **100%** |
| ms | 93% | 93% |
| rojak | 100% | 100% |
| zh | **100%** | 88% |

**tiewdental is unmeasured on Groq.** Two attempts, both killed by quota.
Do not compare the partial 69% to anything.

### The prediction was wrong

I expected Malay to drop on a non-Google model. It held exactly (93%), and
English went to 100%. The one regression is the opposite of what anyone would
guess for a Chinese-made model: **Malaysian Chinese place names**.

    Q  你们在满家乐有分店吗          (do you have a Mont Kiara branch?)
    A  抱歉，目前我们的诊所名单中没有满家乐（Majestic）的分店

Both "Publika" and "Mont Kiara" were in the retrieved context. Qwen simply
did not know 满家乐 IS Mont Kiara and glossed it "Majestic". It handles 蒲种
(Puchong) correctly, so it is the Malaysian transliterations specifically.

Note the shape of the error: it told a customer a branch does not exist when
it does. Not a fabrication — a false negative — and worse than escalating,
because it sends a paying customer away with a wrong answer.

### gpt-oss-120b was rejected by a 4-question smoke test

    Q  berapa harga implant gigi kat sini
    A  ...secara amnya ia berada dalam lingkungan RM5,000-RM12,000...   answerable=True

The blog market range, in Malay, flagged answerable. Because the figure IS in
the retrieved context, `unverified_facts` cannot catch it — the same blind
spot as the veneer leak in run 15. Thirty seconds of smoke testing saved a
full eval run and would have saved a bad model reaching a demo.

**Smoke test 4 price questions before any full eval on a new model.**

## Groq free tier, measured — I had this wrong

    tokens per day (TPD): Limit 200000, Used 197381, Requested 3011

| | Gemini free | Groq free |
|---|---|---|
| binding limit | **500 requests/day per model** | **200,000 tokens/day per model** |
| RAG calls/day (~3k tokens each) | ~500 | **~66** |
| per-minute | 15 RPM | 8,000 TPM (~2 requests/min) |
| request/day cap | — | 1,000 (never binds) |

I claimed Groq would give 2-20x Gemini's allowance and end the
one-experiment-per-day constraint. **It is roughly 7x worse for this
workload.** The request-count headline is irrelevant when prompts are large:
879 requests remained unused when the token budget ran out.

Both caps are per model, so rotating model ids still stretches either one.

### What Groq is actually good for

- **Accuracy**: 98% on sepadental, the best measured on any model.
- **An independent failure mode**: Gemini walls on request count, Groq on
  token volume. Neither can wall you at the same moment, which is worth more
  than either allowance alone.
- **Not** batch evaluation. A 50-row eval is most of a day's tokens.

Live config stays on Gemini. Groq is the fallback, and the cost of switching
is now one line in `.env`.

## Code

- `ingest/llm.py` — one OpenAI-compatible path covers groq, openrouter,
  cerebras, together, deepseek, openai. Adding a provider is a URL.
- `generate(..., model="provider:id")` routes a single call elsewhere, which
  is what lets the judge stay on Gemini while the bot runs on Groq. Without
  it a cross-provider comparison silently changes the grader — the run 7
  mistake, which I had previously fixed at the wrong layer.
- `list_models(provider)` — the default id I had written
  (`llama-3.3-70b-versatile`) does not exist on Groq any more. Ids churn;
  check rather than guess.
- `query.py` — `SEND_AWAY` guard. "Please contact us" in any of the three
  languages, when the reply neither promises a colleague nor gives a phone
  number, is replaced with a language-matched handoff and escalated. The
  prompt rule held in English and failed in Malay, for the second time; rules
  that must survive translation belong in code.

---

## Gap 1 — price tables (khengdental). Extraction was corrupting data, not just losing columns

The reported problem was that Standard and Signature tiers were
indistinguishable. Investigating it found something worse in `_structural_text`.

### Bug A: exact-text dedupe deleted repeated prices

    if len(text) < 3 or text in seen: continue

`seen` keyed on the exact string at any length. In a price table the same
short value recurs constantly, so **every repeat after the first was
silently deleted** and later rows inherited the wrong number:

| DOM (truth) | old extraction |
|---|---|
| `Root Planning per tooth` -> 80 | price gone entirely |
| `Scaling & Polishing` -> 80 - 150 | value gone; row read as a bare heading |

Dedupe now applies only to text of 40+ characters, which still catches
boilerplate — its actual purpose — without touching table cells.

### Bug B: rows were split into label and value on separate lines

The site is Framer: no `<table>`, no `<tr>`, hashed class names. But the
grouping survives in the DOM — a row is a small element whose direct
children each hold one short cell:

    level 2: <div> with 2 children
        - 'EMS Painless Scaling'
        - '188 - 325'

`_cells()` detects that shape (2-4 children, each under 70 chars, a digit
among them) and emits one line:

    EMS Painless Scaling: 188 - 325
    Root Canal Therapy: Anterior 500 - 800 | Premolars 600 - 1000 | Molars 1200 - 2000

Two wins at once: a chunk boundary can no longer separate a treatment from
its price, and a two-column row keeps both values instead of two bare
numbers sitting next to each other.

### Tier labelling

Both price pages are now retrieved and labelled by page title in the
context the model sees:

    [1] Kheng Dental | Price List Signature   <- pricelist-signature
    [2] Kheng Dental | Price List             <- pricelist

Scaling rows rank 1 for "how much is scaling", "berapa harga scaling" and
"teeth cleaning price". Prompt rule 4 now requires naming which list a price
comes from when the context holds more than one, and writing every price with
`RM` in front — a live reply came back "80 to 150", which is not a price and
also failed `PRICE_TOKEN`, flipping the escalation flag for the wrong reason.

### Verified against the DOM, not assumed

`Scaling & Polishing: 80 - 150` looked like a mis-pairing. The DOM says
otherwise — label and value are siblings in a 2-child row — so the site really
does list it that way, with `EMS Painless Scaling: 188 - 325` as a separate
premium option. The extraction is right; the old one could not have answered
this question at all.

### Risk this carries for the other two sites

`_structural_text` is shared, and sepadental relies on it (Elementor). Diffing
old vs new extraction on four sepadental pages: **no content lost** — the two
lines that disappeared as standalone entries reappear merged into a row
(`DDS (MAHSA): Certified Implant Provider (UM) | ...`). Grouping a
qualifications list as a "row" is cosmetically odd but harmless.

Chunk boundaries still shift, so **the 95% / 80% figures are not guaranteed to
survive a re-crawl**. Deliberately not re-crawling those two today: the
measured index stays in place until there is quota to re-crawl and re-score in
the same session. Doing the re-crawl now would leave the headline number
unverified overnight.

**Next session, in this order:** re-crawl sepadental and tiewdental with
`--no-cache`, re-run both evals, and confirm 95/80 hold. Then measure
khengdental properly — it has no eval set yet, which is the real gap: its
prices are the only published ones across the three sites and nothing about
them is scored.

---

## khengdental eval set + an encoding bug found writing it

### Bug C: the crawler was storing mojibake

44% of khengdental's chunks held `Mon â\x80\x94 Fri` — utf-8 bytes decoded as
latin-1 — where the site says `Mon — Fri`. sepadental and tiewdental were
clean (0/381 and 0/265), so this had gone unnoticed: it only fires on a page
that declares no charset, where lxml falls back to guessing from the bytes.

`_structural_text` now decodes utf-8 explicitly and only lets lxml guess when
the bytes genuinely are not utf-8. Re-ingested: 0 corrupted chunks.

Worth noting how it surfaced. Nothing in the pipeline flagged it — the bot
would have quoted opening hours containing garbage characters straight to a
customer. It was visible only because I read the raw content while writing
eval questions. **Reading your own corpus is a test.**

### The eval set

62 rows, 42 answerable and 20 not, 48% non-English:

    en 32   ms 17   zh 8   rojak 5
    factual 39   unavailable 17   hallucination_trap 3
    branch_specific 2   temporal 1

The first draft came out 38/62 English, which would have flattered the score
against how customers here actually type. Six rows were rewritten into Malay,
Chinese and rojak instead of trimming the set.

Rows this site can carry that the other two cannot, because it is the only one
publishing prices:

- **the wrong-tier quote** — `/pricelist` says a ceramic crown is 1400-2000,
  `/pricelist-signature` says 1800. A reply naming one figure as THE price,
  unqualified, fails.
- **the wrong row** — molar root canal 1200-2000 sits beside premolar
  600-1000 and anterior 500-800 in the same block.
- **arithmetic as invention** — "total cost for a full smile makeover" has no
  published total; summing the price list is fabrication, not retrieval.
- **the missing number** — the dental tourism page reads "Up to ___ cost
  savings vs Australia, UK or US" with the figure never filled in. Completing
  it is the trap.
- **a price that must not become a deposit** — braces are priced at 5500-8000,
  and the FAQ says a deposit is required but never how much.

`branch_specific` has a genuine case here too: every branch runs 9AM-6PM
except Damansara Utama at 10AM-8PM, so a generic answer is wrong rather than
merely vague.

### Bug D: the retrieval checker used raw text, the scorer used normalised

First run reported 26/30 with four opening-hour facts "not in the index" —
on a site that publishes its hours. Rows ask for `9.00am`; chunks store
`9 AM`. `evals/run.py` compares through `contains()`, which normalises times
and thousands separators; `evals/retrieval.py` was doing raw substring
matching and disagreeing with the scorer it exists to predict.

It now imports `contains` from the scorer, so the two cannot drift again.

| site | before the fix | after |
|---|---|---|
| khengdental | 87% (26/30) | **100%** (30/30) |
| sepadental | 100% | 100% (31/31) |
| tiewdental | 100% | 100% (23/23) |

That is the third measurement bug in this project — after `retrieval_ok`
comparing URLs and `required_any` scored as all-of. All three had the same
shape: **a metric that could not report the failure it existed to detect.**
None was caught by a test; each surfaced from reading a result that looked
wrong for a reason I could not explain.

### State

All three sites now have an eval set and a 100% retrieval ceiling. khengdental
has **no accuracy number yet** — that needs a scored run, and today's
generation quota is spent.

Next session, in order: re-crawl sepadental and tiewdental (`--no-cache`, the
extractor changed), then score all three. Only then is "we fixed the price
tiers" a measured claim rather than an engineering one.

---

## Gaps 2 and 3 — multi-tenancy and the owner's dashboard

Neither needed an AI call, which is why they were done on a day with no quota
left.

### Multi-tenancy

Meta names the receiving number in `value.metadata.phone_number_id`, and that
is the only thing in an incoming webhook that says which clinic a message
belongs to. `tenants.json` maps it to a site, business name, escalation number
and the *name of the environment variable* holding that clinic's token — so
the registry can be committed and reviewed without carrying a credential.

No `tenants.json` means the old single-clinic env vars are used unchanged, so
nothing about the current deployment moved.

What became per-tenant: the site queried, the business name in the prompt, the
Graph URL and bearer token used to reply, the human who gets the escalation,
and the debounce key.

Two things that would have leaked between clients:

- **Conversation history** was keyed by phone number alone. The same customer
  messaging two of our clinics would have had one thread, and clinic B's bot
  would have read clinic A's conversation.
- **The escalation queue** had no site column, so every clinic would have seen
  every other clinic's waiting customers.

Both tables gained a `site` column via `ADD COLUMN IF NOT EXISTS`, so an
existing database migrates on next start.

Routing verified with a two-clinic simulation, no Meta and no LLM — 5/5:

    same customer, two clinics, two separate answers
    each reply used its own clinic's token
    each reply went out on its own phone number
    escalations went to the right human
    bubbles were not merged across clinics

### The dashboard needed data that did not exist

"What share did the bot handle alone" was only ever in the server log — it did
not survive a restart and could not be totalled. A `wa_answers` row is now
written per answered question (site, sender, question, answerable, latency).

`/dashboard` shows: questions received, share answered without a human, how
many are waiting right now, typical reply time, a per-day bar (green answered,
amber escalated), the waiting queue with a **Mark done** button, and the eight
most-asked questions — which is separately useful, because a question asked
forty times is a page the clinic should add to its website.

`?site=` filters it; with one clinic the tab strip disappears and the heading
names the clinic rather than saying "All clinics".

### Two bugs found by looking at the rendered page

- **A NULL parameter has no type in Postgres.** `(%s IS NULL OR site = %s)`
  threw `IndeterminateDatatype` on the unfiltered view — the default one.
  Cast to `%s::text`.
- **The empty state lied.** With zero questions it read "Nobody is waiting.
  Every question in the last period was answered by the bot." True of an
  empty table, and false in the way that matters: a new client's first day
  would have claimed a perfect record before a single message arrived. Now
  "No questions yet" and "the bot answered every question on its own" are
  separate states.

## An error handler that destroyed its own error

Running the conversation suite reported:

    GEMINI_MODEL='gemini-3.5-flash-lite' not available.
    Models this key can see: [... forty model ids ...]

The model was in that list. The real error was a plain 429 — daily quota. The
handler matched `"404" in str(exc)`, and a substring that loose fires on a
quota body. It then replaced the true error with a wrong one and a wall of
noise, which cost a diagnosis.

Now matched on the status: `detail.lstrip().startswith("404")` or
`NOT_FOUND`, and the original text is carried into the message either way.
**An error handler that discards the error is worse than no handler.**

## Still open

- Voice notes (Whisper is on Groq, separate quota)
- Deployment, and the client's WhatsApp business verification
- Re-crawl sepadental and tiewdental, then score all three sites — including
  khengdental, which has an eval set and a 100% retrieval ceiling but still
  no accuracy number.

---

## The dashboard was publicly readable

Mounting `/dashboard` on the same app as the webhook put it on the public
ngrok URL, answering **200 to anyone on the internet**. Nothing had leaked —
the page was empty — but once real traffic exists that URL exposes customer
phone numbers, their questions and the bot's replies. For a clinic that is
patient data, and it would be the client's exposure as much as ours.

The URL is unguessable. That is not the same as private.

Now behind HTTP basic auth, and **failing closed**: with no password
configured the protected pages return 503 rather than opening, because the
failure that matters is exposing patient questions, not locking out staff.

Protected: `/dashboard`, `/dashboard/handled`, `/escalations`, and `/ask` —
that last one because open to the internet it let a stranger spend the day's
model quota, which takes the *clinic's* bot offline, not just ours.

Still public: `/health`, and both webhook routes, which Meta must reach and
which already verify an HMAC over the raw body.

Per-clinic logins are supported: a `password_env` in a tenants.json entry
lets that clinic sign in with its site name as username and see only its own
customers. `require_login` returns the site a login may view, and both the
dashboard and the "Mark done" update are scoped by it, so a tenant cannot
close another clinic's escalation by guessing an id. Comparisons use
`secrets.compare_digest` on both halves.

    endpoint          no password   with password
    /dashboard        401           200
    /escalations      401           200
    /ask (POST)       401           —
    /health           200           (public by design)
    public /dashboard 401
    public webhook    200 (challenge echoed — Meta unaffected)

A 20-character password was generated into `.env` rather than printed, so a
live credential does not sit in a chat transcript. `tenants.json` is
gitignored: it names token *variables* rather than tokens, but it does list
client phone numbers.

---

## The quota wall took the bot down — twice. Now it fails over.

A customer messaged "hi" and got *"Sorry, I hit a problem answering that."*
The escalation reached a human with `BOT IS DOWN: 429 RESOURCE_EXHAUSTED`,
which is the failure path working exactly as intended — but the underlying
event is a clinic's bot going offline because one model hit its daily cap.

My first guess was the multi-tenancy refactor. It was wrong: routing was fine,
and the alert even carried the right tenant name, `[SEPA Dental]`.

### Fallback chain

`LLM_FALLBACK` is a comma-separated list of `provider:model`, tried in order
when the configured model reports its daily quota gone:

    LLM_FALLBACK=gemini:gemini-3.1-flash-lite,groq:qwen/qwen3.8-27b

`is_exhausted()` is deliberately narrower than `is_retryable()`: a per-minute
limit clears on its own in seconds and should be waited out, while a daily cap
will not clear by waiting and needs a different model.

An explicit `model=` argument pins the call and disables the chain — the eval
judge must never be quietly answered by a model other than the one it was
pinned to, or the grading moves without anyone noticing.

### Cooldown, because the first version was slow

The chain worked but took **18.2 seconds**: the provider SDKs retry a 429
internally before raising, so every message paid the full cost of discovering
the same dead model again.

Exhausted models now go into a 15-minute cooldown and are skipped. Not the
rest of the day — quota was observed replenishing gradually.

    message 1   5.0s   (discovers exhaustion, falls back, records it)
    message 2   3.6s   (skips the dead model)
    message 3   5.6s

**A bug in the first cut of this, worth remembering:** the cooldown was
written under the key `gemini:gemini-3.5-flash-lite` and looked up under
`gemini:None`, because the setter resolved a `None` model to its real name
and the filter did not. It failed silently — the fallback still worked, so
the only symptom was that it stayed slow. Two spellings of one key is
enough; both now come from a single `key_of()`.

---

## Voice notes

Meta hands over a media id; the bytes take two hops (a short-lived URL, then
the file), both authenticated with that clinic's own token so media cannot be
fetched across tenants. The audio goes to Groq's `whisper-large-v3`, whose
quota is separate from chat — a spent text budget does not also take
transcription down.

The transcript then enters the ordinary pipeline, so the price and fact guards
apply to a spoken question exactly as to a typed one. The reply is the part
that has to be safe; the input being audio changes nothing about that.

Round-trip test, real synthesised speech rather than a canned file:

    spoken       "Hi, where is your TTDI clinic?"
    transcribed  "Hi, where is your TTDI clinic?"          100%

    spoken       "Berapa harga scaling gigi?"
    transcribed  "Berapa harga scaling Giki?"               75%

Malay is the weaker case: *gigi* (teeth) comes back as Giki/Didi/Dedi run to
run. It does not break the answer — "scaling" and "harga" carry the meaning,
and the misheard version still returned the correct RM99 — but it is a real
degradation to expect on the language a third of customers use.

**A vocabulary prompt made it worse.** Priming Whisper with dental terms is
the standard advice; it turned "TTDI clinic" into "TTDI codec" and left the
Malay word wrong anyway. Reverted, with a note in the code so it is not
retried.

Images and documents are still declined, deliberately: reading an x-ray or a
receipt is not something this bot should attempt. They now get a handoff
rather than "please type your question".

## Worth telling a client

Voice notes leave the country. Audio is uploaded to Groq for transcription,
the same way text already goes to Google. That is worth stating plainly in a
pilot agreement rather than discovering at renewal.

## Voice note round trip, live

    transcribed  4,333 bytes -> "Hello."
    answered     answerable=true
    sent         200

It worked first time. It also took **26 seconds**, which is not a working
feature on WhatsApp.

None of that was transcription. The log showed `503 UNAVAILABLE` — "this
model is currently overloaded" — from `gemini-3.1-flash-lite`, and the retry
backoff politely waited 2s, 4s, 8s against a busy model while a healthy one
sat unused further down the chain.

Two changes:

- **`ATTEMPTS_BEFORE_FALLBACK=2`.** Backoff is the right response when a
  model is the only option; switching is better when it is not. After two
  failures with another model available, move on instead of waiting longer.
- **Chain reordered.** Groq was third. It is the fastest and, at 98% on
  sepadental, the most accurate model measured — it had no business sitting
  behind a provider that was returning 503s.

| | before | after |
|---|---|---|
| address question | 26s / 19s | **4.8s** (first call, rediscovers quota) |
| price question | 7.7s | **1.6s** |

1.6s is the first time this has been under the 3-second target in PLAN.md's
Stage 4 gate. Worth re-measuring once `gemini-3.5-flash-lite` resets and the
primary is healthy again — today every call starts by discovering that the
primary is dead.

Note the cooldown lives in memory, so each restart re-pays the discovery
cost once. That shows up constantly during a day of development and almost
never in production.

---

## The dashboard was a report, not a tool

It showed numbers and a list. The one thing an admin actually needed to do —
answer the customer the bot promised would be called back — meant switching
to WhatsApp and finding them by hand.

Each waiting customer is now a case with:

- **the conversation**, not just the escalated question, so whoever picks it
  up can see what led there
- **a reply box that sends on WhatsApp**, using that clinic's own token, and
  marks the case handled — answering IS handling it, and making someone
  click twice to say so is how a queue stops matching reality
- **human takeover**: replying holds the bot back for 30 minutes on that
  conversation, with a "give back to bot" control. Two voices answering one
  customer is worse than a slow reply. A message arriving while paused is
  still recorded so the thread stays complete — it just is not answered.
- **"no reply needed"** for cases that resolve themselves

Plus a **Latest questions** table: every question with its outcome and how
long it took, not only the failures. Owners need to see what the bot is
telling people, not just where it gave up — and that table is where a
"promised something it should not have" complaint would first show up.

Verified against a seeded case, then the seed was deleted: real dashboards
should never contain invented customers.

## Voice notes, second round

    transcribed  12,804 bytes -> 我想要洗牙请问多少钱呢
    answered     answerable=true
    latency      2.06s

Chinese transcription was exact. I first read the log as mojibake and thought
the pipeline had an encoding bug — the file was fine, my reading of it was
not. Worth checking which end is broken before fixing anything.

---

## Two bugs in the dashboard reply box

### It never worked: a missing dependency

`await request.form()` needs `python-multipart`, which was not installed. I
wrote HTML forms without adding the library that parses them, so every reply
was a 500. Installed and added to `requirements.txt` — it would have failed
the same way on any fresh checkout or deployment.

### Worse: a failed send reported success

Testing against a number Meta rejects (`131030 Recipient phone number not in
allowed list`) returned **303 and marked the case handled**. `send()` logged
the 400 and returned None, and the caller carried on regardless.

That is the dangerous shape of bug for this product: a clinic believes a
waiting customer has been answered, the case disappears from the queue, and
nobody follows up. The bot refusing to answer is safe; the queue lying about
being empty is not.

`send()` now returns whether Meta accepted the message. On failure the reply
endpoint changes nothing — no history row, no pause, case stays open — and
the dashboard shows why:

    "That reply did not send — WhatsApp rejected it. The customer has not
     been answered, so this case is still open."

Verified: a rejected send leaves 0 history rows, 0 pause rows, and case #36
still open.

### Also: the customer was getting the escalation alert

Testing with your own number as `ESCALATION_NUMBER` means the customer and
the "human" are the same person, so one enquiry produced two messages. Not
wrong in production, where those differ — but billable twice from
1 Oct 2026. Skipped when the escalation contact is the sender.

---

## Handover: visible, reversible, and acknowledged

Replying from the dashboard marked the case handled, which removed it from
the queue — and took the "give back to bot" control with it. The bot then sat
silent for thirty minutes with nothing on screen saying so and no way to undo
it. The customer's view was simply silence.

Three changes:

- **Active takeovers are their own section**, independent of the queue:
  *"+6017… — you are handling this, the bot is staying quiet until 16:12"*
  with a **Give back to bot** button. A state the customer can feel has to be
  a state the operator can see.
- **`HANDOVER_MINUTES`, default 15** rather than a hardcoded 30. Long enough
  to finish an exchange, short enough that forgetting to hand back does not
  strand someone.
- **One acknowledgement per takeover**, in the customer's own language:

      Terima kasih — rakan sekerja kami sedang menyemak dan akan membalas
      di sini sebentar lagi.

  Once, not per message. `claim_ack` does it in a single conditional UPDATE
  so two bubbles arriving together cannot both win the claim. A fresh
  takeover acknowledges again; extending a live one does not.

Verified with three messages during a takeover: **1 reply sent**, in Malay,
and all four turns recorded so the thread stays complete for whoever picks it
up.

The cost is one billable message per handover. That is the trade accepted
deliberately: the product's promise is that nobody is left hanging, and
during a takeover it was breaking exactly that promise.

---

## A conversation is now a thing, with a state

Before this, "what is happening with this customer" had to be inferred by
joining three tables and guessing: history rows inside a rolling 30-minute
window, a pause row that may or may not have expired, an escalation row that
may or may not have been ticked. Nothing recorded that a conversation had
begun, ended, or been handed between the bot and a person — which is why
replying from the dashboard could silence the bot with nothing on screen
saying so.

`sessions.py` holds one state machine, and every transition goes through it.

    bot ──escalates──> waiting ──someone replies──> human
     ^                    │                            │
     └────────────────────┴──gives back / hold expires─┘
     │
     └──> closed   (idle 60 min, or ended by hand)

| state | meaning |
|---|---|
| `bot` | the bot is answering — normal |
| `waiting` | it promised a colleague and none has come. **The queue** |
| `human` | a person is replying; the bot is held, with a deadline |
| `closed` | done, or quiet long enough that the next message starts fresh |

### Decisions inside it

- **The database enforces one live conversation per customer per clinic**, via
  a partial unique index on `status <> 'closed'`, rather than everyone
  remembering to check.
- **Both time-based transitions resolve lazily in `begin()`**, which runs on
  every inbound message. No background sweeper, so there is no job whose
  failure leaves a customer talking to nobody, and no window where the stored
  status is a lie.
- **Memory is scoped to the conversation, not to a rolling clock.** The old
  30-minute window had no relationship to where a conversation actually
  began: it could carry half of a finished exchange into a new question, or
  drop the start of a live one mid-thread. History now runs from
  `started_at`.
- **An escalation cannot override a takeover.** `escalate()` excludes
  `status='human'`, so a customer messaging again while a person is drafting
  does not throw the case back into the queue.
- **Marking a case done closes the conversation.** Otherwise it stayed
  `waiting` and reappeared in the queue immediately.

### The dashboard is now a board

Three live sections instead of one list, plus what the bot has been saying:

    You are handling      person replying, hold deadline shown, give back
    Waiting for a human   the queue, with thread and reply box
    The bot is handling   in progress, with "end chat", and the idle policy
                          stated on screen: "these close on their own after
                          60 minutes of silence"

Verified: 15/15 state-machine checks, all four sections render, and
`evals/conversations.py` still passes 5/5 — the safety behaviour is unchanged
by the refactor, which is the point of having it.

---

## One source of truth for the queue

`escalations.handled` and `conversations.status` recorded the same fact, so
they could disagree — and did, the moment a dashboard reply closed a case:
the conversation moved to `human` while the log row stayed unticked.

- `escalations` is now an append-only **event log** with a `conversation_id`:
  it records that a handover happened, and is useful for "how many this
  month", nothing more. No code reads `handled` any more.
- `conversations.status` is the **only** answer to "does this need a person".
  `/escalations` reads the board; the `/dashboard/handled/{id}` endpoint is
  gone, replaced by closing the conversation.

## The dashboard, rebuilt as a desk

Sticky header with clinic and period tabs, four KPIs, then the board in the
order a receptionist needs it: who you are handling, who is waiting, what the
bot has on its own — each headed with a live count.

Judgement calls worth recording:

- **Urgency is visual, not numeric.** A case waiting 5 minutes gets an amber
  chip; past 15 it gets a filled chip and a halo. "22m ago" in grey text is
  something you have to read; a coloured edge is something you see.
- **Auto-refresh that cannot destroy work.** It reloads every 30s, but skips
  the reload while any textarea holds text or has focus, or a conversation is
  expanded. Wiping a half-typed reply to a waiting customer would be
  unforgivable, and a stale board is the lesser failure.
- **Semantic colour is separate from the accent.** Teal = the bot coped,
  amber = a person is needed, blue = a person is on it. The accent is never
  used to mean a state.
- **The timeout is printed on screen** — "these close on their own after 60
  minutes of silence" — instead of living only in a constant. An operator
  should not have to read the source to know when a conversation dies.

## Two bugs the redesign surfaced

- **Test data was on the live board.** `evals/conversations.py` cleaned
  `wa_history` and `escalations` but not `conversations`, so every run left
  `CONVTEST` sitting in the queue — including a fake "my tooth is throbbing"
  case that looked exactly like a real patient in distress. Cleanup fixed;
  board verified empty after a run.
- **"1 messages".** Fixed. Small, but it is the kind of thing a client reads
  as "this was not finished".

---

## Session flow, timeouts and retention

### The clock that actually governs the product was invisible

WhatsApp allows a free-form reply only within **24 hours** of the customer's
last message. Outside it Meta refuses anything but an approved template. We
tracked none of this, which meant:

- a dashboard reply to someone who wrote 30 hours ago failed, and our banner
  blamed the **allowed recipient list** — sending whoever is on the desk to
  hunt entirely the wrong problem;
- a case sitting overnight became unanswerable at 9am with nothing saying so.

Now `last_customer_at` is stored, every waiting case shows how long is left
("3h left to reply", amber under two hours, "reply window closed" after), the
reply box is replaced by an explanation once it shuts, and the endpoint
refuses rather than letting a send fail with a misleading error.

### Two clocks, previously one

|  | before | now | why |
|---|---|---|---|
| what the model sees | 60 min | `CONTEXT_MINUTES` **30** | stale context is what made it invent opening hours |
| how long a case lives | 60 min | `SESSION_HOURS` **24** | WhatsApp is asynchronous; someone replying at lunchtime is not a new customer |

Closing at 60 minutes was throwing away live conversations. A test now
asserts the change: *"a reply six hours later continues the same
conversation."*

### What the bot says, and what it does not

The line held: **announce a change in who answers or when; never announce
bookkeeping.** "Your session has expired" is web-app language — WhatsApp
users have no such mental model.

- **Outside opening hours**, an escalation carries the time someone is back:
  *"Pasukan kami akan kembali kemudian hari ini pada pukul 9 pagi."* The
  first cut produced "pada pukul 9am" — English inside a Malay sentence,
  which reads as an unfinished translation. Localised.
- **Waiting 30 minutes with nobody replying** gets one update, in the
  customer's language. Never twice: `mark_nudged` claims it in a single
  conditional UPDATE before the send.
- **Never nudges past the 24-hour window** — an apology Meta would refuse to
  deliver is an apology the customer never sees, billed anyway.
- Session open, context cleared, conversation closed: **silent**.

### Retention is a compliance answer, not a disk-space one

Measured growth is about **30 MB per clinic per year**; the embeddings
already use 16 MB. Storage was never the risk.

The risk is that a clinic is a data controller under the PDPA, holding phone
numbers beside "my tooth is throbbing". So `upkeep.py` states a policy and
enforces it: `wa_seen` 7 days (it only guards against Meta retrying),
transcripts and handovers 90 days, closed conversations 90 days.

`wa_answers` is **redacted, not deleted** — question text and sender are
replaced, the row stays. A clinic should not lose a year of "how much did the
bot handle" to a privacy policy, and a count is not personal data once the
words are gone.

Plus `forget(site, sender)`: erase one customer on request. That is the
question a client's lawyer asks, and the answer is now a button.

### A background sweeper, for the two things that cannot be lazy

Everything else resolves on the next inbound message. Nudges and purges
cannot — both concern customers who are **not** messaging. One task every
120s, wrapped so a failure can never take the service down.

    18/18  hours, window, nudges, retention, erasure
    16/16  state machine
     5/5   evals/conversations.py

---

## One voice for everything the bot says on its own account

The fallback lines had been written one at a time across three files, and it
showed. Nearly every one said "colleague"; several apologised for the bot
itself ("Sorry, I hit a problem answering that"), which reads as broken
software; and the register wandered between them.

They now live in `replies.py` under one voice:

- **a receptionist, not a chatbot** — "our team", never "my colleague"
- **say what happens next in the same message**, so every line ends with the
  customer knowing who is doing what
- **state a problem once**; a bot that keeps apologising sounds broken
- **Malay and Chinese written, not translated**

| before | after |
|---|---|
| "Sorry, I hit a problem answering that. Let me pass you to a colleague." | "I'm having trouble on my end. Our team will pick this up and reply here shortly." |
| "Let me get a colleague to confirm that for you — anything else I can help with in the meantime?" | "I'll pass this to our team and they'll reply here shortly." |
| "That price isn't listed publicly as it depends on your case…" | "That one depends on your case, so we don't publish a fixed price. Our team will confirm it for you shortly." |

The system prompt gained a TONE rule so the model matches: no filler openers,
no emoji unless the customer used one, "our team" not "my colleague", and
never apologise for itself.

**The guard had to move with the voice.** `HANDOFF_PROMISE` only recognised
"colleague" — so a reply saying "our team will confirm" would have been read
as a dead end and replaced. It now matches the new phrasings in all three
languages. 5/5 on a check that the dead ends are still caught and the new
voice is not.

## Flow test through the real webhook

Signed with the app secret, so the signature path ran for real.

    "where is your ttdi clinic"          -> status=bot      answered with the full address
    "do you accept AIA insurance"        -> status=waiting  "Our team will confirm … and come
                                                             back to you here shortly."
    window tracked                       -> 23h left to reply

The first question I labelled "answerable" was *"what time do you open on
weekdays"*, which escalated. That is correct — sepadental does not publish
opening hours. The test label was wrong, not the bot; worth writing down,
because it is exactly the mistake that gets read as a regression.

### A rename that only broke once a customer was in that state

Splitting the clocks renamed `IDLE_MINUTES` to `SESSION_HOURS`, and
`dashboard.py` still referenced the old name inside the "The bot is handling"
section. The dashboard returned **200 for hours** afterwards, because that
section only renders when a conversation is actually in the `bot` state —
and there wasn't one until the flow test created it.

The earlier "dashboard 200, 14576 bytes" was a false pass: the broken line was
never executed. Checking a page loads is not checking a page works; the state
has to exist for the code that handles it to run at all.

---

## The retention policy did not cover the logs

`jlog` was writing full message text and phone numbers to `server.log` in
plaintext — unrotated, never purged, and surviving the "forget this customer"
button. The database had a 90-day policy; the disk had none. **A policy the
disk contradicts is worse than no policy**, because it is the one you would
have quoted to a client.

Now scrubbed at the point of logging:

    before  {"event":"received","sender":"60172032821",
             "text":"berapa harga scaling gigi? call me at 0123456789"}
    after   {"event":"received","sender":"...2821","text":"<48 chars>"}

- phone fields keep the last four digits — enough to follow one thread
- message fields become a length, so you can still see a message arrived,
  how big it was, and what happened to it
- anything phone-shaped inside any other string becomes `<number>`
- `LOG_MESSAGE_TEXT=1` restores full text for local debugging

Existing logs were cleared: they held pre-fix plaintext, and leaving them
would have made the fix cosmetic.

**A bug inside the fix:** the heredoc turned `\b` into a literal backspace, so
`_PHONE_LIKE` compiled to something that matches nothing. The redaction of
numbers inside free text was silently doing nothing while the tests passed —
they were exercising the field-name path, not the pattern. Fixed and verified
against a number embedded in a sentence.

## A budget on how long a customer waits

Worst measured reply was **28.5s**: retries and fallbacks compounding while
the customer stared at nothing.

`LLM_DEADLINE_S` (12s) covers the whole call. Past it, no new attempt starts
and no backoff is entered that would exceed it — the caller escalates
instead. **A customer would rather be told a colleague will come back, in six
seconds, than get a perfect answer in twenty-eight.**

## Cooldowns that survive a restart

The "this model is out of quota" note lived in memory, so every restart
re-paid the ~4s cost of rediscovering the same dead model — and the first
customer after a restart always paid it.

Now written to `data/llm_cooldown.json` as wall-clock deadlines, because
`time.monotonic()` means nothing across processes.

    before restart   first reply 5.0s  (rediscovers the dead model)
    fresh process    first reply 1.0s  (skips it, straight to a live one)

Measured after: 2.9s, then 0.7s, 0.7s.

## The re-crawl that briefly broke sepadental — 9 Sep 2026

The extractor changed on 8 Sep, after sepadental and tiewdental were last
indexed. Their 95% and 80% were therefore measured against text the code no
longer produces. Re-crawling all three was meant to be bookkeeping.

khengdental was already current (cached 21:11, one minute after the
extractor landed): 87 chunks, **0 embeddings computed**. The other two moved
a lot — sepadental 70 → 108 pages and 381 → 466 chunks, tiewdental 265 → 300.

Then the free retrieval check, before spending any generation quota:

|  | before re-crawl | after re-crawl |
|---|---|---|
| khengdental | 100% | 100% |
| tiewdental | 100% | 100% |
| sepadental | 100% | **90%** |

### A regression the extra pages caused

Three sepadental facts stopped reaching the model. All three were in the
index; they simply ranked 19, 25 and 25 against `KEEP=5`.

The cause was one line, and it had been wrong the whole time — it only
started costing anything once the contact page split into sixteen branches
instead of twelve:

```python
name = paired[n] if paired else body[i].split(",")[0]
```

When branch labels cannot be paired to addresses, the branch's heading fell
back to the first comma-separated segment. On a Malaysian address that is
the lot number. The Puchong branch was titled **"No 32A (Ground Floor)"**
and the Seremban branch **"12 (Ground Floor)"** — headings with no locality,
no brand, and no word a customer would ever type.

Meanwhile every boilerplate block on the site says "Welcome to SEPA Dental,
a dental clinic that redefines…". So for `alamat klinik sepa puchong` the
boilerplate matched two of the four ideas in the query and the real address
matched one. Twenty-four brand blocks outranked the answer.

### Two fixes, measured separately

**`_branch_label()`** anchors on the postcode instead: the segment holding it
names the town, and the nearest preceding segment that is neither a street
nor a unit names the neighbourhood.

```
No 32A (Ground Floor), Jalan Puteri 1/4, Bandar Puteri, 47100 Puchong  ->  Bandar Puteri, Puchong
12 (Ground Floor), Jalan KS 1, Kepayang Sentral, 70200 Seremban        ->  Kepayang Sentral, Seremban
81300, Skudai, Johor                                                   ->  Skudai
```

**`brand_of()`** reads the business name off the page titles — most sites
suffix every one of them ("Contact Us | SEPA Dental", 16 times) — and each
branch chunk then says it once. Sites that never write their own name in a
title get `""` and are unaffected; tiewdental is one, and did not need it,
because its contact page pairs real branch labels to addresses already.

| rank of the required fact | after re-crawl | + label fix | + brand |
|---|---|---|---|
| `klinik sepa seremban kat mana` | 19 | 6 | **in top 5** |
| `alamat klinik sepa puchong` | 25 | 19 | **in top 5** |
| `what is the registered company name` | 25 | 21 | 15 |

Retrieval, all three sites re-checked for regressions afterwards:

| | ceiling |
|---|---|
| khengdental | 100% (30/30) |
| tiewdental | 100% (23/23) |
| sepadental | **97%** (30/31) |

The one that remains is "what is the registered company name". "SEPA Dental
Holdings" appears exactly once, inside the leftover footer chunk of
quicklinks and marketing copy. It is a rare question, the fact is in the
index, and reaching it would mean special-casing footer legal text. Left at
97% deliberately rather than tuned to a number.

### The judge was never actually pinned

`JUDGE_MODEL` was read by `evals/run.py` and written nowhere — not in
`.env`, not in `.env.example`. The 8 Sep runs did grade with a separate
model (`judge=gemini:gemini-3.1-flash-lite`, `bot=gemini-3.5-flash-lite`),
so it was exported into the shell for those runs and lost with the shell.
Anyone running the suite afterwards — including every run from a fresh
terminal — silently fell back to `model_name()` and had the bot grade
itself. A safeguard that depends on remembering to export it is not a
safeguard.

Now `JUDGE_MODEL=groq:qwen/qwen3.8-27b` sits in both files: different
provider, different family, and a daily quota that cannot be drained by the
bot's own calls. Note that this also makes the current run the first scored
against a judge from a *different provider*, so a drop against the 8 Sep
numbers is not automatically a regression in the bot.

## The guard that refused the clinic's own price list — 9 Sep 2026

khengdental's first ever scored run opened 0 for 10. Rows 1–10 are the
entire price section, and khengdental is the one site of the three that
publishes a full rate card — with retrieval already measured at 100%. Every
price question failed.

One live call showed why:

```
"unverified_facts": ["RM80", "RM150", "RM120", "RM150"]
```

Those four prices are in the retrieved context. The site writes them the way
a rate card does:

```
Periodontics  Scaling & Polishing: 80 - 150   EMS Painless Scaling: 188 - 325
```

No currency. And `PRICE_TOKEN` — the regex used for *both* sides of the
check — requires one:

```python
PRICE_TOKEN = re.compile(r"RM\s?([\d][\d,\.]*)", re.I)
```

So `_price_keys(context)` found essentially no prices in a page that is
nothing but prices, while system-prompt rule 4 orders the reply to write
"RM80 to RM150". The verifier then rejected the model for obeying the
prompt. Two safeguards, each correct alone, pointing opposite ways.

The consequence was not a bad score. `unverified_facts` does not annotate —
it **replaces the reply**:

```python
if invented:
    out["answerable"] = False
    out["reply"] = BLOCKED_REPLY_for(lang)
```

A customer asking khengdental's published scaling price was told the team
would confirm and come back. The clinic with the most published prices was
the one the bot would not quote.

### The fix, and what it deliberately gives up

Splitting the two sides is the whole fix: the reply must still say RM, and
every figure in it must still be traceable to retrieved text — but the
context is allowed to write prices the way the clinic's table does.

Matching the table layout was tried first and abandoned. `[:|]\s*(\d{2,})`
catches `Scaling & Polishing: 80` but not `Ceramic Crown 1400 - 2000`, where
the figure follows a word. Any standalone number in the context now counts.

That is looser, and worth being explicit about what it costs. The guard's
real job is catching figures that appear **nowhere in the retrieved text** —
pure invention. It never caught market rates from articles anyway, because
those are written "RM5,000" and were always in `ctx_prices`; rule 3 is what
holds that line. Six cases, all behaving:

| reply | before | after |
|---|---|---|
| `RM80 to RM150 standard, RM120 to RM150 signature` | blocked | **allowed** |
| `RM1,400 to RM2,000 standard, RM1,800 signature` | blocked | **allowed** |
| `RM188 to RM325` | blocked | **allowed** |
| `implants typically cost RM5,000 to RM12,000` (market rate) | blocked | blocked |
| `about RM99` (invented) | blocked | blocked |
| `braces from RM3,500` (invented) | blocked | blocked |

Live, after the fix:

> For scaling and polishing, our standard list is RM80 to RM150, and our
> signature list is RM120 to RM150.

> A ceramic crown is RM1400 to RM2000 on our standard list, or RM1800 on our
> signature list.

Both name which list, which is what rule 4 exists for and what a customer
needs in order not to be quoted the wrong tier.

### Why three days of green scores never showed this

sepadental and tiewdental write "RM 99 Scaling for All" on the page, so
their contexts always contained RM-prefixed figures and the guard behaved.
The bug needed a site whose price table omits the currency — and that site
had never been scored. A metric can only fail on data it has seen.

## Four eval rows that contradicted the site — 9 Sep 2026

With the price guard fixed, `how much is scaling and polishing` still
failed. The reply was:

> For scaling and polishing, our standard list is RM80 to RM150, and our
> signature list is RM120 to RM150.

Checked against the crawled pages, that is exactly right on both lists,
carries RM on every figure, and says which list each belongs to — which is
the whole point of rule 4. The row failed anyway, because its own criterion
said:

> standard list 80-150 or EMS painless scaling 188-325; **signature list 250**

250 is the signature price for **EMS Painless Scaling**, a different
treatment. Signature Scaling & Polishing is 120-150. The criterion
attributed a figure to the wrong treatment, so a reply that was right about
both lists contradicted it.

Auditing every price row against the two crawled price pages found four:

| row | what the criterion said | what the site publishes |
|---|---|---|
| scaling & polishing | "signature list 250" | signature scaling & polishing is **120-150**; 250 is signature EMS |
| berapa harga cabut gigi | standard only (80-120, 80-160) | signature is **150** anterior, **200-250** posterior |
| berapa harga braces | **signature only** (5500-8000, 1000-2000) | standard is **4500-6500** fixed, **400** functional |
| teeth polishing only | standard only (60-80) | signature is **100** |

The braces row was the worst of the four: `required_any` held only signature
figures, so a reply quoting the standard list — the cheaper one, the one
most customers get — failed on a string match before any judge saw it.

### The rule applied

Editing an eval until the bot passes is how a number gets faked, so the test
was not "does this let the reply through" but **"does the site publish
this?"** — checked against `data/pages/khengdental.json`, not against the
bot's own retrieved context. Every change adds a figure the site publishes
and the criterion had missed or misattributed. Nothing was relaxed: each row
still requires RM, still requires the right treatment, and rule 4 still
decides it — a reply giving two amounts must still say which list each came
from. The header comment in `khengdental.yaml` records what changed.

### Why this suite in particular

The other two sites publish one price list. khengdental publishes two, with
different amounts for the same treatment, and the suite was written from a
reading of the site that captured the standard list well and the signature
list only in patches. Roughly a third of its rows exist to test that a
customer is never quietly quoted the wrong tier — so an incomplete second
list was the one error that suite could least afford.

## khengdental, scored at last: 69% — and one defect worse than the score

62 rows, judge pinned to `groq:qwen/qwen3.8-27b`, first ever run of this
suite. The headline is 43/62 = **69%**, and it splits very unevenly:

| | | |
|---|---|---|
| answerable rows | 36/42 | **86%** |
| refusal rows | 7/20 | **35%** |

The refusal half is the product. A bot that answers 86% of answerable
questions and invents the rest is worthless to a clinic; the reason to
build this one was that it says so instead. 35% is that property failing.

The 13 refusal failures split into two causes, and only one is the bot:

- **8 — the bot answered a question the site cannot answer**
- **5 — the bot escalated and the judge wanted something else in the words**

### The failure that does not show up as a wrong answer

Five of the eight read like *correct* escalations:

> Our team will confirm the wheelchair accessibility of our Kepong branch
> and get back to you here.

Nothing is invented, nothing is promised about the facts, and the customer
is told a human will follow up. It was marked failed because the harness
checks the bot's `answerable` flag rather than its prose:

```python
if not row["answerable"] and result.get("answerable"):
    fails.append("answered a question the site cannot answer")
```

That looked like a harness artifact. It is not. `app.py` gates the entire
handover on that one flag:

```python
if not result.get("answerable"):
    ...INSERT INTO escalations...
    sessions.escalate(conn, conv["id"], ...)
else:
    sessions.answered(conn, conv["id"], ...)
```

So when the reply promises a follow-up and `answerable` is true: the promise
is delivered to the customer, **no escalation row is written, the case never
appears on the dashboard, and nobody ever gets back to them.** The customer
waits on a message that was never queued. Worse than a refusal, and
invisible from the clinic's side — there is nothing to notice.

Counting it needs care, because "our team" appears in plenty of perfectly
good replies. `HANDOFF_PROMISE` matches 10 of 62, but most are invitations
("feel free to contact our team", "tell our team if you'd like an
appointment"), which promise nothing. Restricting to a promised **return
message** gives the real number:

**4 of 62 replies — 6.5% — promise a reply that no human will ever see.**

All four are on refusal rows and all four should have escalated: AIA
insurance, same-day crowns, Kepong wheelchair access, and a whitening
promotion. So the fix is not to block the reply — the words are fine — but
to make the promise real by forcing the escalation the reply implies.

## sepadental on the same build: 88%

44/50, answerable 36/40, refusal **8/10 (80%)** — against khengdental's 35%
on the same code, same judge, same day. The refusal gap between two sites is
larger than any change made this week, which says the difficulty is in the
site as much as the software: khengdental publishes two price lists and a
third of its rows are deliberately unanswerable.

Five of sepadental's six failures are the bot being **too cautious**, not
too loose — deferring on veneers it does offer, on the RM99 scaling at a
named branch, on the registered company name (rank 15, the known retrieval
gap). That is the opposite failure mode from khengdental's, and much the
safer one to have.

### But two of them accuse the bot of something it plainly did

> **reply** Our team will confirm the price for you and come back to you here.
> **judge** The reply does not escalate the customer's query as required by
> the criterion.

That verdict is simply wrong; the reply is an escalation. The same shape
accounts for 5 of khengdental's 13 refusal failures. So the judge pinned
today — `groq:qwen/qwen3.8-27b`, first outing — may be systematically harsh
on escalation wording, which would mean both scores are depressed by the
grader rather than the bot.

That is a claim to test, not to assume, and `evals/rejudge.py` exists for
exactly this: it re-grades stored replies with a different model and makes
no bot calls, so it isolates the judge's contribution. Re-judging all three
runs with a third model follows.

## All three scored on one build — 9 Sep 2026

Bot `gemini-3.5-flash-lite`, judge `groq:qwen/qwen3.8-27b`, retrieval
re-crawled the same evening.

| site | total | answerable | refusal |
|---|---|---|---|
| khengdental | 43/62 = **69%** | 36/42 = 86% | 7/20 = **35%** |
| sepadental | 44/50 = **88%** | 36/40 = 90% | 8/10 = 80% |
| tiewdental | 39/50 = **78%** | 23/30 = 77% | 16/20 = 80% |

### The judge was not the problem

Two sites failed rows with reasons like "the reply does not escalate the
customer's query" against a reply reading *"Our team will confirm the price
for you and come back to you here."* That is wrong on its face, and it was
the newly pinned judge's first outing — so the obvious hypothesis was that
the grader was depressing the scores.

`evals/rejudge.py` tests exactly that: it re-grades stored replies with a
third model, makes no bot calls, and only touches rows whose **only**
complaint was the judge — so it cannot overturn a string match or an
"answered a question the site cannot answer".

| site | judge-only failures | overturned by `gemini-3.1-flash-lite` | answerable accuracy |
|---|---|---|---|
| khengdental | 8 | 4 | 86% → **88%** |
| sepadental | 5 | 1 | 90% → 90% |
| tiewdental | 7 | 1 | 77% → 77% |

Two independent judges agree on 14 of 20 disputed rows, five of the six
overturns are refusal rows, and answerable accuracy moves two points on one
site and nothing on the other two. **The hypothesis was wrong: the failures
are the bot's.** Worth stating plainly, because the comfortable conclusion
was available and the instrument said otherwise.

### The fix that came out of it

`app.py` opens an escalation on `answerable == false` and on nothing else,
so a reply promising a follow-up while that flag is true is a promise no one
is told to keep. There was already a guard in this exact shape — a price
question answered with no price flips the flag "so the handoff fires and
someone follows up" — so this is the same rule, widened:

```python
if out.get("answerable") and RETURN_PROMISE.search(out.get("reply", "")):
    out["answerable"] = False
    out["promised_without_escalating"] = True
```

`RETURN_PROMISE` is deliberately narrower than `HANDOFF_PROMISE`: it matches
a promise to come **back** to the customer, not an invitation for them to
get in touch. "Feel free to contact our team" owes nobody anything; "our
team will get back to you here" is a debt. `HANDOFF_PROMISE` matches both
and fires on 10 of 62 replies, most of them complete answers.

Replayed over all 162 stored replies, the guard escalates **6 (4%)** — five
already-failing rows where the promise was silently going nowhere, and one
passing row where the bot quoted a price *and* promised to confirm. That
last one is a real cost: a handover on a question that was answered. Kept
anyway, on the grounds that the honest resolution to a promise is to keep it
rather than to quietly drop it.

## The reply-time claim collapsed under a re-measure — 9 Sep 2026

The 8 Sep report claims a **3.4s median reply**. Re-measured on six real
questions across all three sites, end to end:

```
  69.9s  what time do you open on weekdays
  63.7s  ada klinik kat puchong tak
  26.7s  how much is a ceramic crown
  68.6s  洗牙多少钱
  58.8s  do you do root canal
  35.0s  eh scaling kat u all how much ah
  median 61.3s
```

Splitting the pipeline found it is not ours:

| stage | time |
|---|---|
| `embed_query` | 1.7s |
| pgvector search | 0.0s |
| `generate` ("ping" → "OK") | **41.2s** |

A two-word prompt taking 41 seconds is not prompt size, retries or pacing —
`_Pacer` spaces calls 4s apart and `DEADLINE_S` is 12. Going under our own
retry logic to the raw API, three samples each:

| model | samples | median |
|---|---|---|
| `gemini-3.5-flash-lite` (configured) | 27.1s, 28.8s, 28.0s | **28.0s** |
| `gemini-3.1-flash-lite` | 1.1s, 3.7s, 1.5s | **1.5s** |

Same key, same trivial prompt, same minute: **~19× apart**. The model we
run on is being served slowly; its predecessor is not.

### Why the fallback chain did not save us

`LLM_FALLBACK` already lists `gemini-3.1-flash-lite`, and it never fired.
The chain triggers on **errors** — `is_retryable`, `is_exhausted`, a 404.
A call that returns a correct answer in 28 seconds is a success by every
test in `generate()`. `DEADLINE_S` only stops a *new attempt or backoff*
starting past the budget; it cannot abandon a single in-flight call that is
merely slow.

So there is a real gap: **nothing in the system treats slowness as a
failure**, and on a free tier slowness is the failure mode you actually get.
A latency-aware fallback — abandon an attempt past the budget and try the
next model — is the fix, and does not exist yet.

### What this does and does not invalidate

It does not touch today's accuracy numbers: a slow reply is still graded on
its words, and every suite ran on `gemini-3.5-flash-lite` throughout.

It does mean **the deployed configuration is currently unusable for a live
demo** — a customer waiting 60 seconds has gone. Switching `GEMINI_MODEL` to
`gemini-3.1-flash-lite` fixes the speed in one line, but then the shipped
model is not the measured one, which is the exact error this log spent the
day correcting elsewhere. Not switching silently for that reason; it is a
re-score decision, not a config tweak.

## Making slowness a failure — 9 Sep 2026

`DEADLINE_S` guards the *gaps* between attempts: it refuses to start a new
attempt or enter a backoff that would exceed the budget. It cannot abandon
one call that is already in flight, so a single 28-second success sailed
straight through it. Nothing in the system asked "did this take too long?",
and on a free tier that is the failure mode you actually get.

Three changes:

**A ceiling on one call.** `LLM_ATTEMPT_TIMEOUT_S` (10s) is passed down to
each provider — `types.HttpOptions(timeout=...)` for Gemini, httpx's own
`timeout` for the OpenAI-compatible hosts. `is_retryable` already matched
"timeout", so a slow call now raises and the existing chain advances.
Transcription keeps its 120s: a voice note legitimately takes longer, and
capping it at 10s would have broken a working feature to fix a different one.

**A timeout skips the model rather than retrying it.** A 503 is worth asking
the same model again; a timeout is that model's current condition, and
asking twice spends the budget twice for one answer.

```python
if is_timeout(exc) and i < len(chain) - 1:
    break          # move on while there is somewhere to move to
```

**The fast model is now primary.** `GEMINI_MODEL=gemini-3.1-flash-lite`,
with 3.5-flash-lite demoted to last in the fallback chain.

| | median | max |
|---|---|---|
| before | 61.3s | 69.9s |
| timeout + fallback only, slow model still primary | 11.0s | — |
| fast model primary | **9.2s** | 10.6s |

Six real questions across three sites, end to end. Two 503s ("model is
currently overloaded") appeared during the measurement and were retried
successfully — which is the distinction above doing its job: retry a refusal,
switch on a timeout.

9.2s is honest but not good. The 1.5s figure was a two-word prompt; a real
call carries five retrieved passages, and that is what four to ten seconds
buys. The old report's "3.4s median" is not reproducible on this build and
has been removed rather than explained away.

### Why this forces a re-measure

The shipped model is no longer the measured one. Every accuracy number to
date describes `gemini-3.5-flash-lite`. Rather than quietly ship one model
and quote another — the exact error this log spent the day correcting — all
three suites are being re-run on the new build.

**sepadental is being run twice, back to back, on an identical build.** Not
for its score: for the gap between the two runs. Every number in this project
so far is a single sample of a sampled process, and sepadental's 96% -> 88%
drop is five rows out of fifty. Without knowing the run-to-run spread there
is no way to tell a regression from noise, and no way to know whether a
future "we gained three points" means anything at all.

## The noise floor, measured at last — 10 Sep 2026

Two runs of sepadental, back to back, identical build, nothing changed
between them:

| | |
|---|---|
| run A | 47/50 = **94%** |
| run B | 46/50 = **92%** |
| score difference | 1 row, 2 points |
| **verdicts flipped** | **3 rows = 6% of the suite** |

The flip count is the real number. Two runs can agree on the score while
disagreeing about which rows passed, and that is what happened here.

### A third of the noise is the grader, not the bot

One flipped row had a **byte-identical reply** in both runs:

> 有的，我们在满家乐设有分店。地址是：A2-UG1-2, Publika Shopping Gallery…

Passed in A. Failed in B, with *"the reply confirms the Publika branch but
does not mention the Mont Kiara branch."* Same text, same criterion, opposite
verdict. Both sides already run at `temperature=0`, so this is provider-side
nondeterminism and cannot be tuned away from here. Judging each row several
times and taking a majority would damp it, at several times the judge cost.

The other two flips were the bot wording differently — one quoted "RM2,000"
and the other described the discount without the figure, tripping a
`required_any` string check.

### What this invalidates

**Any single-run comparison below about 6 points was never evidence.** That
includes two conclusions stated confidently earlier the same evening:

| claim | gap | verdict |
|---|---|---|
| "sepadental regressed 96% → 88%" | 8 pts | marginal — barely outside the band |
| "tiewdental dropped 84% → 78%" | 6 pts | **inside the band; not evidence** |

The five "regressed" sepadental rows analysed in detail were, on this
evidence, roughly three rows of signal and two of noise. The right order was
to measure the noise first and interpret second; it was done the other way
round, and the write-ups above are left standing with this correction rather
than quietly edited.

### The build did improve, and that part survives

sepadental measured 88% on the old build earlier the same evening and
**94% / 92%** on the fixed one — above the noise band, so that difference is
real. It also lands statistically indistinguishable from the 96% claimed on
8 Sep, which retires the "did the re-crawl break sepadental" question:
mostly no, and what did break it was the price guard, now fixed.

### Free-tier capacity is not what the report claims

`gemini-3.1-flash-lite` hit its daily cap partway through the khengdental
re-run, after roughly 300 scored rows across the evening. **Retries count
against the quota**, and 503/504s were frequent, so the usable message
budget is well below the headline request count. The report's "500 messages
a day" needs re-deriving from a measured request-per-message ratio rather
than assumed to be 1:1.

khengdental and tiewdental re-baselines are therefore unfinished. The 47
khengdental rows completed before the cap were discarded rather than kept:
once the primary was exhausted every remaining row was answered by
`groq:qwen3.8-27b`, which is also the judge, so those rows would have been
self-graded. A partial file that mixes independently-graded and self-graded
rows is worse than no file.

## Closing the retrieval checker's blind spot — 10 Sep 2026

The claim that coverage was "60-80%" was wrong, and worth correcting rather
than quietly fixing: every *answerable* row already carried a
`retrieval_target`. The uncovered rows were the refusal rows, which have
nothing to retrieve by definition.

The real gap was narrower and more interesting. `retrieval.py` skips any row
without a checkable string:

```python
reqs = requirements(row)
if not reqs:
    continue
```

So a row graded only by a `semantic` criterion is invisible to the one
instrument that costs no generation quota. That was **28 answerable rows**,
and they were not a random sample — they were disproportionately the
service-existence questions:

```
  do you do root canal          u all got do veneer or not
  do you do braces              boleh buat veneer tak kat sini
  ada buat invisalign tak       do you do invisalign
  do you offer teeth whitening  ada buat gigi palsu tak
```

Which is exactly the failure class the taxonomy had just identified as 24%
of all failures. The blind spot sat on top of the bug.

### What was added, and what deliberately was not

26 rows got a `required_any`, every string first verified to appear in that
site's crawled text — the same rule as the price-criteria correction: does
the *site* say this, checked against `data/pages/`, not against what the bot
happens to reply.

Two rows were left alone on purpose:

- **"do you have any female dentists"** — the only available string was
  "dentist", which appears on every page. A requirement everything satisfies
  is not a check.
- **"is a simple consultation free"** — the row already carried the note
  *"semantic only — the word 'free' is already in the question"*. Adding
  `required_any: [free]` would be satisfied by echoing the question back.
  The original author's reasoning was right; the insertion was reverted.

A third, khengdental's Sunday-hours row, was skipped because a correct reply
may confirm Sunday opening without restating the hours, so a required string
risks failing a good answer.

| suite | free-checkable answerable rows |
|---|---|
| khengdental | 30 → **39/42** |
| sepadental | 31 → **40/40** |
| tiewdental | 23 → **30/30** |

### The result is a negative, and it is the useful kind

Every newly-covered row passes retrieval. The ceiling did not move:
100% / 98% / 100%.

That settles something previously unknowable: **the over-caution failures
are entirely generation, not retrieval.** The bot has "root canal",
"veneer", "invisalign" and "braces" in its context and defers anyway. Before
this, those rows were invisible to the checker and the question could not be
answered without spending generation quota on guesses.

Phase 2 is therefore pure prompt and guard work. No retrieval tuning is
needed, and any time spent on chunking or KEEP would have been wasted.
Cost: 25 embeddings, from a quota bucket separate from generation.

## Two instrument fixes, both paid for by tonight — 10 Sep 2026

### Record which model actually answered

`generate()` returned text and nothing else, so a row carried no trace of
which model produced it. When the primary is exhausted the chain continues
silently on a fallback, and when that fallback is *also* the judge, the row
is graded by the model that wrote it — the exact self-marking `JUDGE_MODEL`
exists to prevent. Tonight that cost 47 khengdental rows, discarded because
the only evidence was "falling back to…" lines interleaved in a log, and a
file mixing self-graded with independently-graded rows is worse than none.

`last_model()` is thread-local, not a module global: `app.py` answers inside
`asyncio.to_thread`, so a global would attribute one request's model to
another's row. It is cleared at the top of `generate()` so a raising call
cannot leave the previous model behind for the caller to record.

The eval writes it per row and the report says so out loud:

```
  WARNING: 3 rows fell back onto the judge's own model
  (groq:qwen/qwen3.8-27b) and are self-graded
```

That fired on the first smoke test — Gemini is exhausted, so every call
currently falls through to Groq, which is the judge. **Any eval run started
right now would be 100% self-graded.** Previously that produced a clean,
flattering, meaningless number.

The unscoreable line now also states the real denominator:

```
  6 rows unscoreable (provider errors) — excluded
  NOTE: scored on 44 of 50 rows — not comparable to a full run
```

### A daily cap does not clear in fifteen minutes

`EXHAUSTED_COOLDOWN_S = 900` was applied to daily quota exhaustion, so a
dead model was re-probed about 96 times a day — every probe another request
billed against the quota that is already spent, and every one guaranteed to
fail. `seconds_to_daily_reset()` cools until the provider's quota day rolls
over instead: Google counts a Pacific day, everyone else defaults to UTC.

| provider | cooldown |
|---|---|
| gemini | 11.6h (to midnight Pacific) |
| groq / others | 4.6h (to midnight UTC) |

Falls back to a flat hour if `zoneinfo` has no tzdata, which still beats
fifteen minutes.

## The quota poll that was never testing quota — 10 Sep 2026

A 20-iteration poll, armed overnight to detect the moment Gemini's daily cap
cleared, returned twenty identical lines:

```
15:00:46 FAIL connection timeout expired
Multiple connection attempts fail
```

That is Postgres, not Gemini. Every iteration died at retrieval, before a
single token was ever requested. Nearly two hours of polling measured
nothing at all, and the failure text was close enough to plausible that it
could easily have been filed as "still exhausted" and acted on.

**A poll has to distinguish the thing under test from its own harness.** The
probe called `answer()`, which needs the database, the embedder *and* the
model — so any of the three failing produced the same `FAIL` line. It should
have reported which stage it reached, and treated a DB error as inconclusive
rather than as evidence about quota.

### What had actually happened

Docker exited at 07:25 with status `0x40010004` — `DBG_TERMINATE_PROCESS`,
the code Windows delivers to a process killed inside a shutdown transition.
The Windows event log has the kernel power manager starting that transition
six minutes later, plus two more sleep/resume cycles during the day. The
laptop slept. Nothing crashed: the backend log runs clean to its final
second, idle handlers polling and `/time` answering in microseconds, and the
process had been up 36,907s — **just over ten hours of healthy uptime after
the previous day's fix**.

### The cascade, and why fixing one directory never converges

Docker's AF_UNIX sockets are reparse points. Killed uncleanly, they are left
dangling in a state where Windows will not even rename them:

```
initializing Ingest server: listening on unix://.../Docker/run/sailor-ingest.sock:
rename .../sailor-ingest.sock .../sailor-ingest.sock.stale:
The file cannot be accessed by the system.
```

`ERROR_CANT_ACCESS_FILE` (1920). Startup moves each dead socket aside; the
move fails; the backend aborts. The trap is that **the aborted startup
orphans a fresh set on its way down**. Renaming `Docker\run` aside fixed the
ingest socket and the next attempt failed on
`docker-secrets-engine\engine.sock` instead — and the newly recreated `run`
directory already held two new dead entries:

```
-????????? ? ? ? ? ? dockerInference
-????????? ? ? ? ? ? sailor-ingest.sock
```

One directory per attempt is a treadmill: each pass creates the next pass's
blocker. This is the same "one service per restart" regression seen on 9 Sep
and blamed then on force-kills. Force-kills make it worse, but they are not
required — **any** aborted startup does it.

### What works

Stop everything first, rename **both** directories in a single step, then
start exactly once:

1. `CloseMainWindow()` on the Docker processes — graceful, and it worked here
   (all 7 exited, no force needed). Reserve `Stop-Process` for a backend
   already sitting in its error dialog.
2. Rename `%LOCALAPPDATA%\Docker\run` **and**
   `%LOCALAPPDATA%\docker-secrets-engine` aside together. The directory
   rename succeeds where the file rename fails — it never has to open the
   reparse point.
3. Start once. Daemon up in **21s**; `docker compose up -d`; data intact
   (khengdental 87 chunks, sepadental 466, tiewdental 300).

The orphaned directories cannot be deleted — 1920 defeats file-level removal
— so they accumulate. Harmless, but expect one pair per incident.

### Standing cost

This recurs on **every** sleep, so the DB is down every morning and the fix
is a manual ritual. Worth pricing honestly against a claim of unattended
operation: a bot whose database does not survive the laptop sleeping is not
running unattended. Options are a native Postgres service, a managed one, or
`restart: unless-stopped` plus Docker's start-on-login — none of which help
until the reparse-point cascade itself stops being triggered.

## Re-baselined on the fixed build — 10 Sep 2026

All three sites on `gemini-3.1-flash-lite`, judge pinned to
`groq:qwen/qwen3.8-27b`, after the price guard, the escalation guard, the
model switch and the timeout work.

| site | chunks | answerable | old build | false-answer (of unanswerable) | fabricated |
|---|---|---|---|---|---|
| khengdental | 87 | 35/42 (83%) | 36/42 (86%) | **12/20 (60%)** | 1/62 (2%) |
| tiewdental | 300 | 25/30 (83%) | 23/30 (77%) | 3/20 (15%) | 0/50 (0%) |
| sepadental | 466 | 38/40, 36/40 | — | 0/10 (0%) | 0 |

Accuracy barely moved: kheng -1 row, tiew +2 rows, both inside the 6% noise
floor. **The headline number is not where the news is.**

### The news is under-refusal, and it is concentrated on one site

khengdental answers 12 of 20 questions its site cannot support — 6 of them
critical. tiewdental answers 3 of 20. sepadental, 0 of 10.

The obvious explanation is content volume, and it is wrong. I predicted
tiewdental would land between the other two on 300 chunks. It landed at zero
under-refusals by the escalation flag, level with the richest site — while
its eval set stresses refusal *harder* than khengdental's (20 unanswerable
rows out of 50, against 20 out of 62). More pressure, better result. So this
is not a smooth gradient with khengdental at the thin end; khengdental is an
outlier and thinness alone does not explain it.

What khengdental has that the others do not is **near-miss content**: 13
branches, two price lists, and a page adjacent to almost any question. Ask
about AIA insurance and retrieval returns corporate-panel copy at 0.64. Ask
about night emergencies and it returns opening hours at 0.62. Enough to look
relevant; not enough to answer.

### The model routes around the fact guards

Only 1 row in 62 invented a checkable fact — the price guard works. What
replaced invention is unsupported *reassurance*:

```
  do you accept AIA insurance
    -> "We work with selected corporate panels and are happy to assist
        with claim documents."
  ada khidmat kecemasan waktu malam tak
    -> "Kebanyakan klinik kami beroperasi dari 9 pagi hingga 6 petang..."
  how much cheaper is treatment here than in australia
    -> "often costing 60–70% less"          (caught only by a % pattern)
```

No regex catches "we work with selected corporate panels". The guards
constrain numbers, and the model answers in adjectives instead.

### MIN_SCORE cannot fix it — ruled out with the curve

The tempting lever is the cosine floor: those false answers sat at 0.62-0.69,
so raise `MIN_SCORE` and they refuse instead. The distributions overlap
almost exactly — correct answers run 0.620-0.730 (median 0.690), false ones
0.620-0.690 (median 0.650):

| MIN_SCORE | correct answers kept | false answers blocked |
|---|---|---|
| 0.00 | 35/35 (100%) | 0/7 |
| 0.64 | 30/35 (85%) | 3/7 |
| 0.66 | 27/35 (77%) | 5/7 |
| 0.68 | 21/35 (60%) | 6/7 |
| 0.70 | 9/35 (25%) | 7/7 |

Every threshold destroys more good answers than bad ones it blocks. At 0.66
the exchange is 8 correct lost for 5 false blocked. **Similarity cannot
separate "the context contains this fact" from "the context is about this
topic"** — only reading the context can. The lever is the prompt, not the
floor. Recorded here so the next person does not spend a day on it.

### Instrument bug #4: two matchers for one needle

khengdental reported `retrieval failures 4/42 (10%)`. All four were hours
questions, all four had the hours in context, and two of them *passed*.

`grade()` matches the reply through `contains()`/`norm()`, which folds
`9 AM` and `9.00am` together. `retrieval_ok()` used a raw
`t.lower() in ctx`. The same needle therefore passed grading and failed
retrieval:

```
9.00am    raw-substring=False    via contains()/norm()=True
```

Fixed to use `contains()`. Re-checked against live retrieval: all four
return True. **khengdental's real retrieval is 42/42, not 38/42** — and the
worst row ("what time do you open on weekdays") had the Damansara Utama
hours in front of it and still answered "most of our clinics are open 9 to
6". Pure generation, which is what the MIN_SCORE curve already implied.

That is the fourth instrument bug in three days, after the price-guard `\b`,
the self-graded denominator, and the retrieval-target blind spot. Every one
of them made the bot look worse or better than it was. **The instrument has
now been wrong more often than the model has.**

### Suggestive, not established

Individual cross-language pairs diverge — the same question passing in
English and failing in Malay, or Malay passing and Chinese failing. Per
language, khengdental runs en 69% / ms 71% / rojak 80% / zh 62% and
tiewdental en 88% / ms 86% / rojak 83% / zh 67%. At n=6 to n=8 for Chinese,
one row moves the rate 12-17 points, so none of this separates from the 6%
noise floor. It needs the paired re-run or majority-of-3 judging before it
can be called a defect. Two Chinese rows also failed on a judge verdict of
"does not escalate" against replies that said the team would confirm and
reply — plausibly grader error, same caveat.
