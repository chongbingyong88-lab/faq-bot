# Eval row schema

One row = one customer question. The scorer reads these fields; nothing is
checked by hand.

```yaml
- question: "how much is scaling"
  lang: en                    # en | ms | zh | rojak
  answer_type: factual        # see below
  answerable: true            # must agree with answer_type; the scorer asserts it
  severity: high              # critical | high | normal
  retrieval_target:           # url fragments, ANY of which counts as the right
    - "rm99-scaling-for-all"  # source. Omit for rows with no correct source.
  evaluation:
    required: ["99"]          # ALL of these must appear
    required_any: ["implant", "植"]   # at least ONE must appear
    forbidden: ["Invisalign"] # none may appear
    forbidden_pattern: "RM ?[0-9]"   # regex that must not match
    semantic: "RM99 must refer to scaling and polishing, not another treatment"
  note: "why this row exists"
```

`required` is AND, `required_any` is OR. Use `required_any` whenever a fact can
be phrased more than one way — a bilingual reply may say "implant" or "植", and
an address may be identified by street, postcode or phone.

## Matching is normalised, not literal

Comparisons run on both sides after normalisation, so rows stay readable
instead of turning into 25 hand-written regexes:

- lowercased, whitespace collapsed
- **times** — `5:00 PM`, `5 pm`, `5.00pm` all normalise to `5.00pm`, so
  `required: ["5.00pm"]` matches every spelling
- **thousands separators** — `RM2,000` and `RM2000` both become `rm2000`
- **numeric terms are bounded** — a `required` value made only of digits and
  dots must not be flanked by other digits. `"99"` therefore does NOT match
  `1999`, and `"100"` does NOT match `RM1,100`.

`forbidden_pattern` is a regex applied case-insensitively to the normalised
reply.

## Anti-parroting

A `required` term that already appears in the question proves nothing — *"Sorry,
I have no information about Invisalign"* contains "Invisalign". Every row must
assert something the question does **not** contain, or carry a `semantic` rubric
that demands an affirmative answer. The preflight check flags rows that only
assert words echoed from their own question.

## answer_type

| value | meaning | answerable |
|---|---|---|
| `factual` | plain fact stated on the site | true |
| `branch_specific` | correct answer differs per branch — a generic reply is wrong | true |
| `temporal` | promo or dated claim; must not invent an expiry | true |
| `unavailable` | the site genuinely does not say | false |
| `hallucination_trap` | site contains adjacent-but-wrong text (blog market rates, masked `RM5xxx`, a discount that is not a price) | **either** |
| `ambiguous` | under-specified; a good reply asks which branch/treatment | true |

`hallucination_trap` names the *failure mode*, not answerability, so it is the
one type that can be either. Most are `false` — the site has no real answer and
only a lookalike. A few are `true`: *"is dr darren yap available"* is answerable
in part (he does practise there) while the trap is the half that isn't (the site
lists no schedule). Every trap row must carry a `forbidden`, `forbidden_pattern`
or `semantic` guard naming what must not be said; the scorer enforces that.

## severity

- `critical` — a wrong reply invents a price, opening hour or policy. This is
  the number a business owner cares about.
- `high` — a confidently wrong fact (wrong address, wrong branch hours).
- `normal` — unhelpful or missed, but harmless.

## What the scorer computes

Separating retrieval from answering, so a failure says *why*:

- **accuracy** on answerable rows (`required` present, `forbidden` absent, semantic pass)
- **false-answer rate** — `answerable:false` rows where the bot answered anyway
  or emitted a `forbidden` string. Weighted by severity.
- **refusal accuracy** — `answerable:false` rows correctly escalated
- **retrieval failure rate** — `retrieval_target` absent from the retrieved
  chunk urls. Distinguishes "never retrieved it" from "retrieved it and still
  answered wrong".
- **per-language accuracy** — en / ms / zh / rojak, so tuning MIN_SCORE on
  English cannot silently break Malay.
- **per-answer_type accuracy**

`semantic` rubrics go to a separate LLM judge that sees only the question, the
rubric and the reply — never the retrieved context, or it will rationalise a
wrong answer as correct.
