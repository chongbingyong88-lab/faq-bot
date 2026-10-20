# Build plan

Stage by stage. Each stage has a **gate** — don't start the next one until
it's met. Pricing and pitching are deliberately last.

Realistic timeline: 2–3 weeks part-time. Not one week.

---

## Stage 0 — Pick targets (half day)

Choose ONE vertical and three real businesses in it. Everything downstream
is tuned for that vertical, so switching later costs you Stage 3 again.

Ranked by repetitive DM volume:
1. **Vets** — vaccination price, spay cost, is the doctor in, walk-in or appointment
2. **Dental** — scaling price, braces packages, opening hours
3. **Florists / gift delivery** — WooCommerce sites crawl beautifully, and
   deadline questions ("can you deliver tomorrow, do you cover Puchong")
   are brutal around Raya, Mother's Day and Valentine's

Qualify each candidate site before committing:

```bash
python check_site.py https://candidate.com.my
```

Under ~150 extracted words = JS-rendered or a one-pager. Skip it.
Also open `/sitemap.xml` — 15+ pages is a good target.

**Fallback worth planning for:** many Malaysian SMEs have no real website,
just Instagram and WhatsApp. If that's what you find, the ingestion input
isn't a URL — it's a PDF price list or an exported chat history. Same
chunker, different loader.

**Gate:** three URLs written down, all passing the check.

---

## Stage 1 — Validate ingestion ✅ done

Three real Malaysian dental sites are ingested and queried daily: khengdental
(87 chunks), sepadental (466), tiewdental (300). The gate is met — chunks
have been read by hand, repeatedly, and two ingestion defects were found and
fixed that way rather than by the eval: contact pages labelled by lot number
instead of neighbourhood, and price rows that lost which of two price lists
they came from.

The original warning below is kept because it was right: it did break.

> The pipeline exists but has only ever run against synthetic markdown. It has
> never touched a real Malaysian business site. Assume it breaks.

Start with one large, content-heavy anchor site (a telco or airline help
centre) before the small local ones. If the bot can't answer from a 200-page
help centre, the bug is yours. If you start on a thin local site and get bad
answers, you won't know whether it's your chunking or just a thin website.

**Gate:** all three sites ingested; you have personally read ~30 chunks and
agree each could answer a real customer question.

---

## Stage 2 — Eval harness

50 questions per business, written from their own site. Include ~10 the site
genuinely cannot answer.

Script outputs two numbers:
- **Accuracy** on answerable questions
- **False-answer rate** — how often it invented something instead of escalating

The second number matters more. An owner will forgive "let me check with my
colleague". They will not forgive a bot quoting a price that doesn't exist.

**Gate:** one command prints both numbers.

---

## Stage 3 — Tune until the numbers are good

All the knobs are in Stage 1: chunk size, `MIN_PACK_WORDS`, rerank threshold,
prompt wording, how many chunks you pass. Change one at a time and re-run
the eval. Expect the first run to be worse than you hope.

**Gate:** ≥90% on answerable, **zero** invented prices or hours.
Do not proceed on a bad number — that number IS the pitch.

⚠️ **Not met, 10 Sep 2026.** sepadental 95%/90% on a paired run; khengdental
83%; tiewdental 83%. Invented facts are close to zero (kheng 1 row in 62,
tiew 0 in 50) — the price guard did that. The gate fails on the other half:
khengdental answers 12 of 20 questions its site cannot support. See TUNING.md
for why `MIN_SCORE` is not the fix (the curve is measured, and every
threshold costs more good answers than it blocks bad ones).

---

## Stage 4 — FastAPI service

Wrap `answer()` in an HTTP endpoint. Deploy public on Railway or Fly.
No messaging channel yet.

**Gate:** curl it from your phone's mobile data and get an answer in under 3s.

⚠️ **Not met.** Median reply is 9.2s (down from 61.3s once the reranker went
and the timeout chain was fixed, but still 3x the gate), and it runs locally
behind ngrok rather than deployed on Railway or Fly. Stages 5 and 6 were
built on top of an unmet gate — worth saying out loud, since the plan's own
rule is not to start the next stage until the gate passes.

---

## Stage 5 — WhatsApp adapter

Meta gives every dev app a free test number: unlimited messages to up to 5
verified recipients, no business verification, no app review. Note that once
a number is added to the list of 5 it **cannot be removed** — don't burn slots.

Build: webhook, HMAC verify (`X-Hub-Signature-256`), return 200 immediately,
process async, dedupe on message ID, debounce rapid-fire messages.

**Gate:** you message it from your own phone and get a sane reply.

---

## Stage 6 — Survive real humans

This is where a demo becomes a product:
- Voice notes and images (people send these constantly here)
- Malay / English / Chinese code-switching in one message
- Someone sending five separate bubbles instead of one message
- Human handoff trigger and an escalation log

Let friends abuse it for a week.

**Gate:** a full week with no reply you'd be embarrassed to show an owner.

---

## Stage 7 — Demo polish

- Ingest-any-URL in under 10 minutes, so you can prep a prospect's own bot
  before the meeting. This is the thing that actually closes.
- Small log dashboard: questions asked, % escalated, response times
- Keep the web chat UI as a fallback for anyone not in your 5 numbers

**Gate:** you can go from "here's their URL" to a working bot in one sitting.

---

## Stage 8 — Pricing and pitch (last)

Deferred on purpose. You can't price what you haven't measured.

One thing to gather *early* though, because it's free and shapes everything:
when you're at a vet or clinic anyway, ask how many WhatsApp enquiries they
get a day and who answers them. That's customer discovery, not selling.

Market reference gathered so far:
- Platform SaaS (WATI etc.): RM299–799/month — what clients compare against
- Custom build: RM5,000–15,000 typical for production-ready
- Suggested first-client rate: RM3,000–4,000 setup + RM500–700/month,
  framed as a founding-client rate for a testimonial and case study
- Meta message fees billed separately on the client's own WABA, itemised.
  From 1 Oct 2026 every reply is chargeable — never bundle this.
- Check msmedigital.my for the MSME Madani Digital Grant (reported 50%
  matching, up to RM5,000). Verify terms; grant windows open and close.

---

## Note

Stages 1–4 produce something worth showing regardless of whether anyone
buys: a real retrieval system with a measured accuracy number. Stages 5–7
are what make it a business. Don't skip 2 and 3 to get to 5 faster — the
eval number is the entire difference between this and a ManyChat reseller.
