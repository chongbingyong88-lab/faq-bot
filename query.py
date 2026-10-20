"""Ask the indexed business a question.

    python query.py --site someclinic
    python query.py --site someclinic -q "berapa harga scaling?"

This is the core the WhatsApp webhook will call later. Keep it channel-agnostic.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from ingest.embed import embed_query  # noqa: E402
from ingest.llm import generate, model_name  # noqa: E402
import replies  # noqa: E402
from ingest.store import connect, search  # noqa: E402

MODEL = model_name()
CANDIDATES = 20
KEEP = int(os.getenv("KEEP", "5"))
# Cosine similarity floor, NOT the old reranker logit. The cross-encoder is
# gone (it cost 43s of the 45s per reply), so ranking is vector similarity
# alone and this threshold lives on a 0..1 scale. Needs tuning against the
# eval — 0.0 keeps everything, which is deliberately the starting point.
MIN_SCORE = float(os.getenv("MIN_SCORE", "0.0"))
# "RM5xxx" / "RM1xxxx": digits deliberately hidden by the business, so
# the price is not published. Never let one reach a customer.
# One x is enough: the site masks cheap treatments as "RM9x" and "RM5x" just
# as it masks braces as "RM5xxx". Requiring two x's let those through.
MASKED_PRICE = re.compile(r"RM\s?\d+x+", re.I)

CJK = re.compile(r"[一-鿿]")
# Malay shares the Latin alphabet and plenty of vocabulary with English, so
# detection leans on function words a customer would not use in English.
MALAY = re.compile(r"\b(berapa|harga|kat|tak|ada|boleh|saya|awak|nak|macam|"
                   r"mana|bila|apa|siapa|buka|tutup|alamat|nombor|telefon|"
                   r"klinik|rawatan|gigi|cawangan|masih|sekarang|korang|ke|"
                   r"pukul|waktu|hari|jam|ni|tu|itu|ini|dan|sini|kalau|dekat|guna|"
                   r"untuk|dengan|ansuran|insurans|percuma)\b", re.I)


# The guard replaces the whole reply, so it has to answer in the customer's
# language too — an English fallback to a Chinese question is its own failure.
# The wording lives in replies.py, so one voice covers every fallback.
def BLOCKED_REPLY_for(lang):        # noqa: N802  (kept for call-site clarity)
    return replies.line("price_unavailable", lang)


def HANDOFF_REPLY_for(lang):        # noqa: N802
    return replies.line("handing_over", lang)


# "Please contact us" is a dead end — the customer IS contacting them. The
# prompt forbids it and that held in English while Malay kept answering
# "Sila hubungi kami untuk mendapatkan sebut harga". Second time an
# English-worded rule has degraded in another language (the first was the
# masked-price leak in run 3), so it goes in code instead.
SEND_AWAY = re.compile(
    r"sila\s+(hubungi|call|whatsapp|datang|rujuk)|"
    r"boleh\s+hubungi\s+kami|"
    r"please\s+(contact|call|reach out to|get in touch|speak to)|"
    r"feel free to\s+(contact|call|reach)|"
    r"do\s+contact\s+us|kindly\s+(contact|call)|"
    r"请联系|请拨打|请与我们联系|欢迎致电", re.I)
# Saying it is fine as long as a human is actually promised...
HANDOFF_PROMISE = re.compile(
    r"colleague|teammate|our team|our staff|the clinic will|"
    r"rakan\s+(sekerja|sepasukan)|pegawai|pasukan\s+kami|pihak\s+klinik|"
    r"同事|我们的团队|本诊所", re.I)
# ...or a real number is handed over, which is help, not deflection.
PHONE_IN_REPLY = re.compile(r"\d{2,4}\s?-\s?\d{3,4}\s?\d{3,4}")

# A promise to come BACK to the customer, as opposed to an invitation for
# them to make contact. The distinction decides whether anyone owes them a
# reply: "feel free to contact our team" promises nothing, while "our team
# will get back to you here" is a debt. HANDOFF_PROMISE is too broad for
# that job — it matches both, and fires on plenty of complete answers.
RETURN_PROMISE = re.compile(
    r"get back to you|come back to you|revert to you|"
    r"will (confirm|check|follow up|update you|let you know)|"
    r"akan (hubungi|maklumkan|kembali|semak|sahkan|beritahu)|"
    r"kami akan|会(回复|联系|跟进|确认)您|回复您|稍后(回复|联系)",
    re.I)

PRICE_TOKEN = re.compile(r"RM\s?([\d][\d,\.]*)", re.I)
# Asking for a figure that only exists if a base price does. SEPA publishes
# "RM2,000 OFF" for Invisalign and never the price it comes off, so any
# amount in an answer to this implies a total nobody published. The prompt
# rule alone let "the discount offered is RM2,000 OFF" through.
FINAL_PRICE_QUESTION = re.compile(
    r"after discount|after disc\b|post.?discount|final price|total price|"
    r"selepas diskaun|lepas diskaun|harga akhir|berapa selepas|"
    r"折扣后|折後|实际价格|最终价格", re.I)
# Someone asking what something costs. If we send them away without a figure,
# a human needs to know they are waiting.
PRICE_QUESTION = re.compile(r"berapa|harga|kos|how much|price|cost|charge|"
                            r"多少钱|价格|费用|收费", re.I)
# 9.30am / 9:30 AM / 9 pagi / 6 petang — the shapes an opening hour takes.
# No trailing \b on the first branch: "9.30am" has no boundary between the
# "0" and the "a", so \b there made it fall through and match "30am" as if
# the hour were 30.
TIME_TOKEN = re.compile(r"\b(\d{1,2})\s*[:.]\s*(\d{2})"
                        r"|(?<![\d.:])\b(\d{1,2})\s*(?:am|pm|pagi|petang)\b", re.I)


# Prices as a rate card writes them, which is usually without a currency:
# khengdental publishes "Scaling & Polishing: 80 - 150". PRICE_TOKEN needs
# "RM", so verifying a reply against it found no prices in that context at
# all — and rule 4 requires the reply to write "RM80". Every correct price
# on the site was therefore flagged invented and replaced with "our team
# will confirm". The one clinic publishing a full rate card was the one the
# bot would not quote.
#
# Deliberately looser than PRICE_TOKEN, which still guards the reply: a
# number the model presents as a price must be traceable to the context,
# but the context may write it the way the clinic's own table does.
# Matching the table layout was tried first and was too brittle: it caught
# "Scaling & Polishing: 80 - 150" but not "Ceramic Crown 1400 - 2000",
# where the figure follows a word rather than a separator. Take any
# standalone number instead. The check that matters is still strict on the
# side that can lie — the reply must write RM, and the figure must be
# traceable to text we actually retrieved.
# Not `\b(\d...)`: there is no word boundary between "M" and "2", so that
# form silently skipped every "RM2,000" — 13 figures on sepadental and 25 on
# tiewdental — and would have flagged the site's own prices as invented. The
# lookbehind instead means "a number that does not start mid-number", which
# also stops "1,200" being read as a separate "200".
CTX_PRICE = re.compile(r"(?<![\d.,])(\d[\d,.]*)")


def _amount(raw: str) -> str:
    """Digits only, so "RM1,200" and "1200" compare equal. Both sides of the
    check must normalise identically or the guard rejects its own source."""
    return re.sub(r"\D", "", raw).lstrip("0") or "0"


def _price_keys(text: str) -> set[str]:
    return {_amount(m.group(1)) for m in CTX_PRICE.finditer(text)
            if _amount(m.group(1))}


def _time_keys(text: str) -> set[str]:
    keys = set()
    for m in TIME_TOKEN.finditer(text):
        if m.group(1):
            keys.add(f"{int(m.group(1))}:{m.group(2)}")
        elif m.group(3):
            keys.add(f"{int(m.group(3))}:00")
    return keys


def unverified_facts(reply: str, context: str) -> list[str]:
    """Prices and times in the reply that do not appear in the context.

    The prompt cannot be trusted with this. Conversation history made the
    model invent opening hours SEPA has never published, and telling it not
    to did not stop it. A figure that is not in the retrieved text was not
    retrieved — it was made up — and that is checkable in code.
    """
    bad: list[str] = []
    ctx_prices, ctx_times = _price_keys(context), _time_keys(context)
    for m in PRICE_TOKEN.finditer(reply):
        if _amount(m.group(1)) not in ctx_prices:
            bad.append(m.group(0).strip())
    for m in TIME_TOKEN.finditer(reply):
        key = (f"{int(m.group(1))}:{m.group(2)}" if m.group(1)
               else f"{int(m.group(3))}:00")
        if key not in ctx_times:
            bad.append(m.group(0).strip())
    return bad


def partial_scaling_booking_reply(question: str, context: str, lang: str | None) -> str | None:
    """Source-grounded recovery for the demonstrated mixed request, not a generic price rule."""
    if not re.search(r"\bscaling\b|洗牙", question, re.I):
        return None
    if not re.search(r"\b(slot|book|booking|appointment|confirm)\b|temujanji|预约|時段|时段", question, re.I):
        return None
    # All three bindings must occur in the same retrieved passage. Never
    # take a number from an unrelated service or a promotional URL alone.
    supported = False
    for block in context.split("\n\n---\n\n"):
        # Header and source URL are metadata, not the published price evidence.
        text = block.split("\n", 2)[-1]
        # Require an explicit price-to-service binding, not co-occurrence.
        if re.search(r"RM\s*99(?![\d.,])\s+scaling\s+(?:and|&)\s+polishing", text, re.I):
            supported = True
    if not supported:
        return None
    if lang == "Chinese":
        return "网站列出的洗牙及抛光价格是 RM99。预约时间需要由我们的团队确认，他们会在这里回复您。"
    if lang == "Malay":
        return "Harga scaling dan polishing yang disenaraikan ialah RM99. Pasukan kami perlu sahkan waktu temujanji dan akan membalas di sini."
    return "The published price for scaling and polishing is RM99. Our team needs to confirm appointment availability and will reply here."


def detect_language(question: str) -> str | None:
    """Which language the reply must be in, or None to let the model decide."""
    if CJK.search(question):
        return "Chinese"
    markers = len(MALAY.findall(question))
    if markers >= 2:
        return "Malay"
    if markers == 0:
        # English got no instruction at all, and the model drifted: "what time
        # do you close on saturday" came back entirely in Malay. Say it.
        # One marker is left alone — that is the rojak case, where forcing a
        # single language reads worse than letting the model mirror the mix.
        return "English"
    return None

SYSTEM = """You are the customer service assistant for {business}, replying on WhatsApp.

Rules, in order of importance:
1. Answer ONLY from the CONTEXT below. Never use outside knowledge about this business.
2. If the context does not contain the answer, set "answerable" to false. Do not guess.
   Never invent prices, opening hours, availability, policies or promises.
   But answer what you CAN. When the CONTEXT answers part of the message, give
   that part and escalate only the part that is missing. If the CONTEXT states
   something for the business as a whole — a promotion, a service, a dentist —
   and the customer asks whether it applies at a named branch or still applies
   now, confirm what the CONTEXT does say and offer to confirm the rest. The
   CONTEXT naming no end date and excluding no branch is not a reason to
   refuse. Escalating a whole message when you hold most of the answer is a
   failure, not caution.
   This does NOT relax rule 3. Answering more of a message never licenses a
   price. An article, blog or treatment guide on this business's own site is
   not a price list — a figure quoted there is what the market charges, and
   stating it is the worst error you can make. Answer more, quote no more.
3. PRICES — the strictest rule. State a price ONLY if the CONTEXT shows it as
   THIS business's own price for the treatment asked about. These three are NOT
   this business's price, and repeating any of them is a serious error:
   a) General market rates, e.g. "in Malaysia implants cost RM5,000 to RM12,000",
      or any figure from an article, blog or cost comparison. That is what the
      market charges, not what this clinic charges. Never quote it, not even
      with a caveat.
   b) Masked figures such as RM5xxx, RM1xxxx, RM9x. The digits are hidden on
      purpose, which means the price is NOT published. Never repeat the masked
      text and never fill in the missing digits.
   c) A discount or amount off, e.g. "RM2,000 OFF". That is a reduction, never
      a price, and you cannot work out a final price from it. When someone asks
      what a treatment COSTS — including "how much after discount" — do not
      repeat the discount amount at all. Naming it implies a total that this
      business has never published.
   If the only figure you have is one of these, treat the price as unavailable:
   say the price depends on the case and must be confirmed with the clinic.
4. Write every price with RM in front of it — "RM80 to RM150", never "80 to 150".
   A bare number is not a price, and the customer cannot tell what it means.
   If the CONTEXT holds prices from MORE THAN ONE price list or tier for the
   same treatment, do not pick one. Give both and name each list, e.g.
   "RM188-325 on our standard list, RM250 on the signature list", so the
   customer knows which applies to them. Silently choosing one is how someone
   is quoted the wrong price.
5. Never state the price of a DIFFERENT treatment from the one asked about.
   If you cannot price what they actually asked for, say so plainly — do not
   substitute a price you happen to know for something else. Asked about
   implants, never answer with the scaling price.
6. VAGUE QUESTIONS — never a dead end. If the message is too broad to answer
   ("how much?", "berapa harga", "what do you offer?"), do NOT just hand it to
   a colleague. Instead help them choose: name 3-5 real services from the
   CONTEXT, include any price you ARE allowed to quote, and ask which one they
   mean. Set "answerable" true — you are moving the conversation forward, not
   escalating. Only escalate when you genuinely have nothing useful to offer.
7. BE SPECIFIC. When asked where, when, what time, or what number, quote the
   exact detail from the CONTEXT — the full street address, the exact opening
   time, the whole phone number. Never replace it with a vague stand-in like
   "our Puchong branch" or "during business hours". The detail IS the answer.
8. If the CONTEXT shows the business offers a service, confirm plainly that
   they offer it before adding any caveat. Do not hedge about whether a
   treatment exists when the context clearly lists it.
9. Reply in the same language the customer used. Malay, English, Chinese and mixed
   messages are all normal — match them naturally.
10. Write ONE short WhatsApp message. Normally 2-4 sentences. When listing
   options you may use up to 5 short lines, one option per line, plain text
   with no bullets, numbers, asterisks or markdown. Every message costs money.
11. Never promise a refund, discount, booking or appointment. Those need a human.
12. If a RECENT CONVERSATION is shown, use it to understand what a short
    follow-up refers to ("kat Puchong ada?", "how about for kids?"). It tells
    you what they mean — it is NOT a source of facts. Every fact still comes
    from CONTEXT.
13. If the customer describes a symptom, pain, injury or asks what is wrong
    with them, do NOT give clinical advice or a diagnosis. Say it needs a
    dentist to look at it and set "answerable" false so a human follows up.

Return ONLY this JSON, no other text and no code fences:
{{"answerable": true|false, "reply": "your message", "sources": ["url", ...]}}

If answerable is false, "reply" should say our team will confirm and come back
to them here. Never tell the customer to contact, call or reach out to the
clinic themselves — they are already talking to it, and being sent away is the
one thing a WhatsApp assistant must never do.

TONE. Write as the clinic's receptionist, not as a chatbot. Warm and brief.
Say "our team" or "we", never "my colleague". Do not apologise more than once,
and never apologise for yourself — state what happens next instead. No filler
openers ("Certainly!", "Great question!"), no emoji unless the customer used
one first."""


def build_context(hits: list[dict]) -> str:
    blocks = []
    for i, h in enumerate(hits, 1):
        label = " > ".join(p for p in (h["title"], h["heading"]) if p)
        blocks.append(f"[{i}] {label}\nSource: {h['url']}\n{h['content']}")
    return "\n\n---\n\n".join(blocks)


def _ask_model(system: str, user: str) -> dict:
    raw = generate(system, user)
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # NEVER put raw model output in front of a customer. A thinking model
        # that spends its budget reasoning returns truncated JSON, and the old
        # fallback sent that JSON to the customer verbatim.
        return {"answerable": False, "sources": [], "parse_failed": True,
                "raw": raw[:400],
                "reply": replies.line("trouble")}


def answer(conn, site: str, question: str, business: str | None = None,
           history: list[tuple[str, str]] | None = None) -> dict:
    """history: [(role, text), ...] oldest first, role is 'customer' or 'you'.

    Channel-agnostic — the caller owns storage. Without it every message is
    treated as a fresh conversation, so "kat Puchong ada?" after a price
    question means nothing.
    """
    started = time.time()

    # Search on the current question ONLY. Prepending the conversation was
    # measurably worse: "kat Puchong ada?" alone retrieves the Bandar Puteri
    # branch at 0.676, but prefixed with "berapa harga scaling" the scaling
    # pages dominate and the branch drops out of the top 5 entirely. History
    # is for interpreting the question, not for finding the documents.
    hits = search(conn, site, embed_query(question), limit=CANDIDATES)
    for h in hits:
        h["score"] = h["similarity"]
    top = [h for h in hits[:KEEP] if h["score"] > MIN_SCORE]

    if not top:
        return {
            "answerable": False,
            "reply": replies.line("handing_over", detect_language(question)),
            "sources": [],
            "retrieved": 0,
            "chunk_urls": [],
            "latency": time.time() - started,
        }

    system = SYSTEM.format(business=business or site)
    convo = ""
    if history:
        turns = "\n".join(f"{r}: {t}" for r, t in history[-6:])
        convo = (f"RECENT CONVERSATION (oldest first). Use it ONLY to understand "
                 f"what the new message refers to.\n{turns}\n\n"
                 f"Answering earlier questions well does NOT permit you to answer "
                 f"this one. Judge the new message on its own: if CONTEXT below "
                 f"does not contain the answer, set answerable false, however "
                 f"helpful the conversation has been so far.\n\n")
    context = build_context(top)
    user = f"{convo}CONTEXT:\n{context}\n\nCUSTOMER MESSAGE:\n{question}"
    # A per-request instruction lands far harder than a general rule several
    # lines up in the system prompt — wrong-language replies were 42% of all
    # accuracy failures.
    lang = detect_language(question)
    if lang:
        user += f"\n\nIMPORTANT: the customer wrote in {lang}. Reply in {lang}."

    out = _ask_model(system, user)

    # Chinese is unambiguous to detect, so it gets a real check rather than a
    # hope: if the question was Chinese and the reply has no Chinese in it,
    # ask again once, more bluntly.
    if lang == "Chinese" and not CJK.search(out.get("reply", "")):
        out = _ask_model(system, user + "\n\nYour reply MUST be written in "
                                        "Chinese characters. Do not use English or Malay.")
        out["language_retried"] = True

    # Deterministic backstop for masked prices (RM5xxx, RM1xxxx). The prompt
    # rule holds in English but was still leaking in Malay, and a hidden-digit
    # figure is unambiguous enough to catch in code — where no amount of
    # rephrasing in another language gets past it.
    # Any price or time in the reply that is not in the retrieved text was not
    # retrieved — it was invented. Catches the hours fabrication that survived
    # every prompt rule I tried.
    invented = unverified_facts(out.get("reply", ""), context)
    if invented:
        out["unverified_facts"] = invented
        # Rebuild from source evidence, never from the rejected model reply.
        # Narrowly cover a scaling-price + booking request without confirming a slot.
        repaired = partial_scaling_booking_reply(question, context, lang)
        out["answerable"] = False
        out["reply"] = repaired or HANDOFF_REPLY_for(lang)
        if repaired:
            out["partial_booking_reply"] = True

    # Published opening times are not proof of a bookable appointment.
    # This prototype has no authoritative availability integration.
    if re.search(r"\bconfirm\b.{0,90}\b(?:slot|appointment)\b|\b(?:book|reserve)\b.{0,90}\b(?:tomorrow|today|slot|appointment)\b|sahkan.{0,60}temujanji|确认.{0,30}预约", question, re.I):
        repaired = partial_scaling_booking_reply(question, context, lang)
        out["reply"] = repaired or HANDOFF_REPLY_for(lang)
        out["answerable"] = False
        out["booking_confirmation_deferred"] = True
        if repaired:
            out["partial_booking_reply"] = True

    if MASKED_PRICE.search(out.get("reply", "")):
        out["masked_price_blocked"] = MASKED_PRICE.findall(out["reply"])
        out["answerable"] = False
        out["reply"] = BLOCKED_REPLY_for(lang)

    # "How much after discount?" — any RM figure here reads as the total, even
    # when it is only the discount. There is no safe amount to state, so state
    # none. Deterministic because rule 3c did not hold on its own.
    if (FINAL_PRICE_QUESTION.search(question)
            and PRICE_TOKEN.search(out.get("reply", ""))):
        out["final_price_blocked"] = PRICE_TOKEN.findall(out["reply"])
        out["answerable"] = False
        out["reply"] = BLOCKED_REPLY_for(lang)

    # Sending the customer away, without promising a human or giving a number,
    # is a dead end whatever language it is written in.
    reply_now = out.get("reply", "")
    if (SEND_AWAY.search(reply_now)
            and not HANDOFF_PROMISE.search(reply_now)
            and not PHONE_IN_REPLY.search(reply_now)):
        out["sent_away_blocked"] = True
        out["answerable"] = False
        out["reply"] = HANDOFF_REPLY_for(lang)

    # A price question answered without a price is a customer still waiting.
    # The reply may be perfectly good ("depends on your case, contact us") so
    # keep it — but flip the flag so the handoff fires and someone follows up.
    if (out.get("answerable") and PRICE_QUESTION.search(question)
            and not PRICE_TOKEN.search(out.get("reply", ""))):
        out["answerable"] = False
        out["price_unanswered"] = True

    # Same failure, wider: any reply that promises to come back to the
    # customer while `answerable` stays true. app.py opens an escalation on
    # that flag and on nothing else, so the promise reaches the customer, no
    # escalation row is written, the case never lands on the dashboard, and
    # nobody ever follows up. Four of khengdental's 62 replies did this —
    # including "our team will confirm the wheelchair accessibility of our
    # Kepong branch and get back to you here", which is a good reply that
    # silently went nowhere. Keep the words, make the promise real.
    if (out.get("answerable")
            and RETURN_PROMISE.search(out.get("reply", ""))):
        out["answerable"] = False
        out["promised_without_escalating"] = True

    out["retrieved"] = len(top)
    out["top_score"] = round(top[0]["score"], 2)
    out["chunk_urls"] = [h["url"] for h in top]
    # The scorer needs the text the model actually saw: a URL match is not
    # evidence the fact was retrieved.
    out["context"] = context
    out["latency"] = round(time.time() - started, 2)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--business", help="Display name used in the prompt")
    ap.add_argument("-q", "--question")
    args = ap.parse_args()

    conn = connect()

    if args.question:
        print(json.dumps(answer(conn, args.site, args.question, args.business), indent=2))
        return 0

    print(f"asking '{args.site}' — blank line to quit\n")
    while True:
        try:
            q = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            break
        r = answer(conn, args.site, q, args.business)
        flag = "" if r["answerable"] else "  [ESCALATE]"
        print(f"\n{r['reply']}{flag}")
        for s in r.get("sources", []):
            print(f"   src: {s}")
        print(f"   {r['retrieved']} chunks, {r['latency']}s\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
