"""HTTP service + WhatsApp adapter.

    uvicorn app:app --port 8000

Endpoints
    GET  /health            db reachable, config present
    POST /ask               {"site","question"} -> the answer() dict
    GET  /webhook/whatsapp  Meta's verification challenge
    POST /webhook/whatsapp  incoming messages

The webhook answers 200 before doing any work. Meta retries aggressively
on a slow response, and every retry is a duplicate reply to the customer.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, Request, Response
from fastapi import Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

load_dotenv()

from ingest.store import connect  # noqa: E402
import dashboard  # noqa: E402
import desk_auth  # noqa: E402
import sessions  # noqa: E402
import replies  # noqa: E402
import upkeep  # noqa: E402
from ingest.llm import transcribe  # noqa: E402
from query import detect_language  # noqa: E402
from query import answer  # noqa: E402

WA_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WA_PHONE_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WA_VERIFY = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WA_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
WA_SITE = os.getenv("WHATSAPP_SITE", "sepadental")
WA_BUSINESS = os.getenv("WHATSAPP_BUSINESS", "")
GRAPH_ROOT = "https://graph.facebook.com/v21.0"


@dataclass(frozen=True)
class Tenant:
    """One clinic. Meta tells us which by the number the message arrived on."""
    phone_id: str
    site: str
    business: str
    token: str
    escalation: str
    dash_password: str = ""

    @property
    def graph(self) -> str:
        return f"{GRAPH_ROOT}/{self.phone_id}/messages"


DEFAULT_TENANT = Tenant(WA_PHONE_ID, WA_SITE, WA_BUSINESS, WA_TOKEN,
                        os.getenv("ESCALATION_NUMBER", ""))


def load_tenants() -> dict[str, Tenant]:
    """Read tenants.json, or fall back to the single-clinic env vars.

    Tokens stay in the environment and the file only names which variable
    holds each one, so the registry can be committed and reviewed without
    carrying a credential.

        [{"phone_number_id": "123", "site": "sepadental",
          "business": "SEPA Dental", "token_env": "WHATSAPP_TOKEN",
          "escalation_number": "60..."}]
    """
    path = Path(__file__).resolve().parent / "tenants.json"
    if not path.exists():
        return {WA_PHONE_ID: DEFAULT_TENANT} if WA_PHONE_ID else {}
    out: dict[str, Tenant] = {}
    for row in json.loads(path.read_text(encoding="utf-8")):
        pid = str(row["phone_number_id"])
        out[pid] = Tenant(
            phone_id=pid,
            site=row["site"],
            business=row.get("business", ""),
            token=os.getenv(row.get("token_env", "WHATSAPP_TOKEN"), ""),
            escalation=row.get("escalation_number", ""),
            # A clinic signs in with its site name and sees only its own
            # customers. Only the operator password sees every tenant.
            dash_password=os.getenv(row["password_env"], "")
            if row.get("password_env") else "",
        )
    return out


TENANTS = load_tenants()


def tenant_for(phone_id: str) -> Tenant:
    """Never guess. An unknown number answering as the wrong clinic would
    quote one client's prices to another's customer."""
    t = TENANTS.get(phone_id)
    if t is None:
        jlog("unknown_phone_id", phone_id=phone_id,
             known=sorted(TENANTS), falling_back_to=DEFAULT_TENANT.site)
        return DEFAULT_TENANT
    return t

# People send five short bubbles instead of one sentence. Wait this long for
# more from the same number, then answer the lot once — which is also what
# keeps the bill down, since Meta charges per reply from 1 Oct 2026.
DEBOUNCE_S = float(os.getenv("WHATSAPP_DEBOUNCE_S", "2.0"))

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
log = logging.getLogger("faqbot")

app = FastAPI(title="faq-bot")
_pending: dict[str, list[str]] = {}
_timers: dict[str, asyncio.Task] = {}


# The retention policy covers the database and said nothing about the log,
# which was quietly keeping full message text and phone numbers in plaintext,
# unrotated, and surviving the "forget this customer" button. A policy the
# disk contradicts is worse than no policy.
LOG_MESSAGE_TEXT = os.getenv("LOG_MESSAGE_TEXT", "").lower() in ("1", "true", "yes")
_PHONE_FIELDS = {"sender", "to", "phone_id", "from"}
_TEXT_FIELDS = {"text", "question", "reply", "bot_reply", "body", "raw"}
_PHONE_LIKE = re.compile(r"\b\d{7,15}\b")


def _scrub(key: str, value):
    """Keep a log worth reading without keeping what a customer said."""
    if key in _PHONE_FIELDS and isinstance(value, str) and value:
        return f"...{value[-4:]}"          # enough to follow one thread
    if key in _TEXT_FIELDS and isinstance(value, str):
        if not LOG_MESSAGE_TEXT:
            return f"<{len(value)} chars>"
        return _PHONE_LIKE.sub("<number>", value)
    if isinstance(value, str):
        return _PHONE_LIKE.sub("<number>", value)
    if isinstance(value, list):
        return [_scrub(key, v) for v in value]
    return value


def jlog(event: str, **fields) -> None:
    safe = {k: _scrub(k, v) for k, v in fields.items()}
    log.info(json.dumps({"event": event, "ts": round(time.time(), 3), **safe},
                        ensure_ascii=False))


# ------------------------------------------------------------------ dedupe
SEEN_SQL = """
CREATE TABLE IF NOT EXISTS wa_seen (
    msg_id TEXT PRIMARY KEY,
    seen_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS wa_history (
    id BIGSERIAL PRIMARY KEY,
    sender TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    at TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE wa_history ADD COLUMN IF NOT EXISTS site TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS wa_history_sender_idx
    ON wa_history (site, sender, at DESC);
CREATE TABLE IF NOT EXISTS escalations (
    id BIGSERIAL PRIMARY KEY,
    sender TEXT NOT NULL,
    question TEXT NOT NULL,
    bot_reply TEXT,
    reason TEXT,
    at TIMESTAMPTZ DEFAULT now(),
    handled BOOLEAN DEFAULT false
);
-- Added after the single-clinic build: one deployment now serves several,
-- and a shared queue would show one clinic another's customers.
ALTER TABLE escalations ADD COLUMN IF NOT EXISTS site TEXT NOT NULL DEFAULT '';
-- An append-only record of every time the bot handed over. Whether anyone
-- has DEALT with it is conversation state, not a second flag here: two
-- places recording one fact is two places that can disagree, and this one
-- disagreed the moment a reply closed the case.
ALTER TABLE escalations ADD COLUMN IF NOT EXISTS conversation_id BIGINT;
-- Every answered question, so the owner's dashboard can say what share the
-- bot handled alone. That number was previously only in the server log,
-- which means it did not survive a restart and could not be totalled.
CREATE TABLE IF NOT EXISTS wa_answers (
    id BIGSERIAL PRIMARY KEY,
    site TEXT NOT NULL,
    sender TEXT NOT NULL,
    question TEXT NOT NULL,
    answerable BOOLEAN,
    latency_ms INTEGER,
    at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS wa_answers_site_idx ON wa_answers (site, at DESC);
-- While a human is handling someone, the bot must stay out of the way.
-- Two voices answering one customer is worse than a slow reply.
CREATE TABLE IF NOT EXISTS wa_paused (
    site TEXT NOT NULL,
    sender TEXT NOT NULL,
    until TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (site, sender)
);
-- One acknowledgement per takeover, not one per message: the customer needs
-- to know a person has it, and then needs to be left alone.
ALTER TABLE wa_paused ADD COLUMN IF NOT EXISTS acked BOOLEAN NOT NULL DEFAULT false;
"""
SEEN_SQL += sessions.SCHEMA

# A conversation goes stale: two questions an hour apart are not one thread.
# DEFAULT 0 = memory OFF. It works, but it also made the bot more willing to
# answer: after two helpful turns it invented opening hours SEPA has never
# published, and strengthening the prompt did not stop it. Turn it on with
# HISTORY_TURNS=6 once evals/conversations.py passes.
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "0"))
HISTORY_WINDOW_MIN = int(os.getenv("HISTORY_WINDOW_MIN", "30"))
# Where "let me check with a colleague" actually goes. Without this, escalation
# is a polite dead end and the customer waits forever.
ESCALATION_NUMBER = os.getenv("ESCALATION_NUMBER", "")
# How long the bot stays out of the way after a human replies from the
# dashboard. Long enough to finish an exchange, short enough that forgetting
# to hand back does not leave a customer talking to nobody.
HANDOVER_MINUTES = int(os.getenv("HANDOVER_MINUTES", "15"))


def load_history(conn, site: str, sender: str,
                 since=None) -> list[tuple[str, str]]:
    """The turns of THIS conversation.

    Was a rolling 30-minute window, which had no relationship to where a
    conversation actually began — it could carry half of a finished one into
    a new question, or drop the start of a live one mid-thread.
    """
    if HISTORY_TURNS <= 0:
        return []
    if since is None:
        rows = conn.execute(
            "SELECT role, text FROM wa_history WHERE site=%s AND sender=%s "
            "AND at > now() - make_interval(mins => %s) "
            "ORDER BY at DESC, id DESC LIMIT %s",
            (site, sender, HISTORY_WINDOW_MIN, HISTORY_TURNS)).fetchall()
    else:
        rows = conn.execute(
            "SELECT role, text FROM wa_history WHERE site=%s AND sender=%s "
            "AND at >= %s ORDER BY at DESC, id DESC LIMIT %s",
            (site, sender, since, HISTORY_TURNS)).fetchall()
    return [(r[0], r[1]) for r in reversed(rows)]


def save_turn(conn, site: str, sender: str, role: str, text: str) -> None:
    conn.execute(
        "INSERT INTO wa_history (site, sender, role, text) VALUES (%s,%s,%s,%s)",
        (site, sender, role, text))


def already_seen(msg_id: str) -> bool:
    """True if we have handled this message id before.

    Meta redelivers on any hiccup, so this is what stops a customer getting
    the same answer three times. Persisted, not in-memory, so a restart
    mid-retry does not resend.
    """
    with connect() as conn:
        conn.execute(SEEN_SQL)
        cur = conn.execute(
            "INSERT INTO wa_seen (msg_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (msg_id,))
        return cur.rowcount == 0


# ------------------------------------------------------------------ upkeep
SWEEP_SECONDS = int(os.getenv("SWEEP_SECONDS", "120"))


async def sweep_once() -> None:
    """Nudge people who have waited too long, and purge what we do not keep.

    These are the only two jobs that cannot be done lazily on the next
    inbound message: both are about customers who are NOT messaging us.
    """
    def _due():
        with connect() as conn:
            conn.execute(SEEN_SQL)
            return sessions.needing_nudge(conn)

    for conv in await asyncio.to_thread(_due):
        tenant = next((t for t in TENANTS.values()
                       if t.site == conv["site"]), DEFAULT_TENANT)
        # Claim it before sending, so a slow send cannot double-nudge.
        def _claim(cid=conv["id"]):
            with connect() as conn:
                return sessions.mark_nudged(conn, cid)
        if not await asyncio.to_thread(_claim):
            continue
        from query import detect_language
        lang = detect_language(conv["last_question"] or "")
        text = upkeep.nudge_text(lang)
        if not upkeep.is_open():
            text += upkeep.when_back(lang)
        if await send(conv["sender"], text, tenant):
            await asyncio.to_thread(record_outgoing, conv["site"],
                                    conv["sender"], text)
            jlog("nudged", site=conv["site"], sender=conv["sender"])

    def _purge():
        with connect() as conn:
            conn.execute(SEEN_SQL)
            return upkeep.purge(conn)
    if (gone := await asyncio.to_thread(_purge)):
        jlog("retention_purge", **gone)


async def sweeper() -> None:
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            await sweep_once()
        except Exception as exc:          # never let upkeep kill the service
            jlog("sweep_failed", error=str(exc)[:300])


@app.on_event("startup")
async def _start_sweeper() -> None:
    asyncio.create_task(sweeper())


# --------------------------------------------------------------------- auth
# The webhook must be reachable by Meta, so this whole app is on a public
# URL. Everything that shows a customer's phone number or question therefore
# needs a login — an unguessable URL is not a private one.
DASH_USER = os.getenv("DASHBOARD_USER", "admin")
DASH_PASS = os.getenv("DASHBOARD_PASSWORD", "")
_basic = HTTPBasic(auto_error=False)

def desk_logins():
    values = {DASH_USER: (DASH_PASS, None)} if DASH_PASS else {}
    for t in TENANTS.values():
        if t.dash_password and t.site not in values:
            values[t.site] = (t.dash_password, t.site)
    return values

desk = desk_auth.DeskAuth(app, desk_logins)



def require_login(
    request: Request,
    creds: HTTPBasicCredentials | None = Depends(_basic),
) -> str | None:
    """Returns the site this login may see; None means every site.

    Fails closed. With no password configured the protected pages are shut
    rather than open, because the failure that matters here is exposing
    patient questions, not locking out staff.
    """
    session = desk.read(request)
    if session is not None:
        return session['allowed']
    tenant_pw = {t.site: t.dash_password
                 for t in TENANTS.values() if t.dash_password}
    if not DASH_PASS and not tenant_pw:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Set DASHBOARD_PASSWORD in .env to open the dashboard.")

    def unauthorised():
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in",
            headers={"WWW-Authenticate": 'Basic realm="faq-bot"'})

    if creds is None:
        raise unauthorised()
    # compare_digest on both halves: a plain == leaks length and prefix
    # through timing, and this is reachable from the open internet.
    if DASH_PASS and secrets.compare_digest(creds.username, DASH_USER)             and secrets.compare_digest(creds.password, DASH_PASS):
        return None
    for site, pw in tenant_pw.items():
        if secrets.compare_digest(creds.username, site)                 and secrets.compare_digest(creds.password, pw):
            return site
    raise unauthorised()


def session_state(site: str, sender: str) -> dict:
    """Open the conversation this message belongs to and return its state.

    Resolving expiries lives in sessions.begin(), so an expired takeover
    becomes the bot's turn simply by a message arriving — there is no
    background job whose failure would leave a customer talking to nobody.
    """
    with connect() as conn:
        conn.execute(SEEN_SQL)
        return sessions.begin(conn, site, sender)


# Said once when a human has taken over, in the customer's own language.
# Silence during a handover reads as being ignored, which is the one thing
# this bot is supposed to never do.
def claim_ack(conv_id: int) -> bool:
    with connect() as conn:
        conn.execute(SEEN_SQL)
        return sessions.claim_ack(conn, conv_id)


def record_incoming(site: str, sender: str, text: str) -> None:
    """Keep the thread complete even when the bot says nothing."""
    with connect() as conn:
        conn.execute(SEEN_SQL)
        save_turn(conn, site, sender, "customer", text)


def record_outgoing(site: str, sender: str, text: str) -> None:
    with connect() as conn:
        conn.execute(SEEN_SQL)
        save_turn(conn, site, sender, "you", text)


# ------------------------------------------------------------------- health
@app.get("/health")
def health() -> dict:
    db, err = True, None
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        db, err = False, str(exc)[:200]
    return {
        "ok": db,
        "db": db,
        "db_error": err,
        "whatsapp_configured": bool(WA_SECRET) and any(
            t.token and t.phone_id for t in TENANTS.values()),
        "tenants": [{"site": t.site, "business": t.business,
                     "phone_id": t.phone_id[-4:], "token": bool(t.token),
                     "escalation": bool(t.escalation)}
                    for t in TENANTS.values()],
    }


# ---------------------------------------------------------------------- ask
class Ask(BaseModel):
    site: str
    question: str
    business: str | None = None


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request, site: str | None = None, days: int = 14,
                   error: str | None = None,
                   allowed: str | None = Depends(require_login)) -> HTMLResponse:
    """What the clinic owner opens. Read-only apart from Mark done."""
    days = max(1, min(days, 90))
    if allowed is not None:
        site = allowed          # a tenant login cannot look at another clinic
    with connect() as conn:
        conn.execute(SEEN_SQL)
        data = dashboard.collect(conn, site, days)
        sites = [r[0] for r in conn.execute(
            "SELECT DISTINCT site FROM wa_answers WHERE site <> '' "
            "ORDER BY site").fetchall()]
    known = [allowed] if allowed is not None else sorted({t.site for t in TENANTS.values()} | set(sites))
    return HTMLResponse(dashboard.render(data, known, site, days, error, csrf_token=(desk.read(request) or {}).get('csrf', '')))


@app.post("/dashboard/reply")
async def dashboard_reply(request: Request,
                          allowed: str | None = Depends(require_login)):
    """Answer the customer from here, instead of switching to WhatsApp.

    This is the point of the queue: the bot promised a colleague would come
    back, and this is the colleague coming back.
    """
    form = await request.form()
    site = allowed or str(form.get("site") or "")
    sender = str(form.get("sender") or "")
    text = str(form.get("text") or "").strip()
    if not (sender and text):
        return RedirectResponse("/dashboard", status_code=303)
    if len(text) > 2000:
        return RedirectResponse("/dashboard?error=length", status_code=303)

    tenant = next((t for t in TENANTS.values() if t.site == site),
                  DEFAULT_TENANT)

    # WhatsApp only allows a free-form reply for 24 hours after the customer
    # last wrote. Sending anyway returns an error that reads like a wrong
    # phone number, which sends whoever is on the desk hunting the wrong
    # problem. Refuse it here and say what actually happened.
    def _conv():
        with connect() as conn:
            conn.execute(SEEN_SQL)
            return sessions.get(conn, site, sender)
    conv = await asyncio.to_thread(_conv)
    if not conv or not sessions.can_reply_freely(conv):
        jlog("reply_window_closed", site=site, sender=sender)
        return RedirectResponse(
            f"/dashboard?error=window{'&site=' + site if site else ''}",
            status_code=303)

    delivered = await send(sender, text, tenant)
    if not delivered:
        # Do not mark it handled and do not silence the bot: as far as the
        # customer is concerned nothing happened, and the queue must agree.
        jlog("human_reply_failed", site=site, sender=sender)
        return RedirectResponse(
            f"/dashboard?error=send{'&site=' + site if site else ''}",
            status_code=303)

    def _persist():
        with connect() as conn:
            conn.execute(SEEN_SQL)
            save_turn(conn, site, sender, "you", text)
            # Answering IS handling it. The conversation moving to 'human' is
            # the whole record of that; there is no second flag to keep in
            # step.
            sessions.begin(conn, site, sender)
            sessions.take_over(conn, site, sender, text)
    await asyncio.to_thread(_persist)
    jlog("human_replied", site=site, sender=sender, chars=len(text))
    return RedirectResponse(f"/dashboard?site={site}" if site else "/dashboard",
                            status_code=303)


@app.post("/dashboard/resume")
async def dashboard_resume(request: Request,
                           allowed: str | None = Depends(require_login)):
    """Hand the customer back to the bot before the 30 minutes are up."""
    form = await request.form()
    site = allowed or str(form.get("site") or "")
    sender = str(form.get("sender") or "")

    def _resume():
        with connect() as conn:
            conn.execute(SEEN_SQL)
            sessions.hand_back(conn, site, sender)
    await asyncio.to_thread(_resume)
    jlog("bot_resumed", site=site, sender=sender)
    return RedirectResponse(f"/dashboard?site={site}" if site else "/dashboard",
                            status_code=303)


@app.post("/dashboard/forget")
async def dashboard_forget(request: Request,
                           allowed: str | None = Depends(require_login)):
    """Erase one customer, on request. The PDPA answer, and the thing a
    client asks for before signing anything."""
    form = await request.form()
    site = allowed or str(form.get("site") or "")
    sender = str(form.get("sender") or "")

    def _forget():
        with connect() as conn:
            conn.execute(SEEN_SQL)
            return upkeep.forget(conn, site, sender)
    gone = await asyncio.to_thread(_forget)
    jlog("customer_erased", site=site, sender=sender, removed=gone)
    return RedirectResponse(f"/dashboard?site={site}" if site else "/dashboard",
                            status_code=303)


@app.post("/dashboard/close")
async def dashboard_close(request: Request,
                          allowed: str | None = Depends(require_login)):
    """End a conversation the bot is handling, so the next message starts
    fresh instead of inheriting this one's context."""
    form = await request.form()
    site = allowed or str(form.get("site") or "")
    sender = str(form.get("sender") or "")

    def _close():
        with connect() as conn:
            conn.execute(SEEN_SQL)
            sessions.close(conn, site, sender)
    await asyncio.to_thread(_close)
    jlog("conversation_closed", site=site, sender=sender, by="dashboard")
    return RedirectResponse(f"/dashboard?site={site}" if site else "/dashboard",
                            status_code=303)


@app.get("/escalations")
def escalations(limit: int = 20, site: str | None = None,
                allowed: str | None = Depends(require_login)) -> dict:
    """Customers waiting for a person, straight from the state machine.

    Reads conversations rather than the escalation log: the log records that
    a handover happened, the conversation records whether anyone has since
    dealt with it. Asking the log produced a queue that still listed people
    who had already been answered.
    """
    if allowed is not None:
        site = allowed
    with connect() as conn:
        conn.execute(SEEN_SQL)
        waiting = sessions.board(conn, site)["waiting"][:limit]
    return {"count": len(waiting), "site": site, "waiting": [
        {"conversation": c["id"], "site": c["site"], "sender": c["sender"],
         "question": c["last_question"], "bot_reply": c["last_reply"],
         "reason": c["reason"], "messages": c["messages"],
         "since": c["started_at"].isoformat(),
         "waiting_since": (c["escalated_at"] or c["last_at"]).isoformat()}
        for c in waiting]}


@app.post("/ask")
async def ask(body: Ask,
              allowed: str | None = Depends(require_login)) -> dict:
    # Open to the internet, this endpoint let anyone spend the day's model
    # quota — which takes the clinic's bot offline, not just ours.
    if allowed is not None and body.site != allowed:
        raise HTTPException(status_code=403, detail="not your site")
    started = time.time()
    result = await asyncio.to_thread(
        _answer_sync, body.site, body.question, body.business)
    jlog("ask", site=body.site, question=body.question,
         answerable=result.get("answerable"), top_score=result.get("top_score"),
         latency=round(time.time() - started, 2),
         chunk_urls=result.get("chunk_urls", []))
    return result


def _answer_sync(site: str, question: str, business: str | None,
                 sender: str | None = None) -> dict:
    # A fresh connection per call: answer() runs in a worker thread and a
    # psycopg connection must not be shared across threads.
    with connect() as conn:
        conn.execute(SEEN_SQL)
        conv = sessions.begin(conn, site, sender) if sender else None
        # The model sees the recent part of the conversation, not all of a
        # session that may legitimately span a day. Stale context is what
        # made it invent opening hours after a few helpful turns.
        since = None
        if conv:
            cutoff = datetime.now(timezone.utc) - timedelta(
                minutes=sessions.CONTEXT_MINUTES)
            since = max(conv["started_at"], cutoff)
        history = load_history(conn, site, sender, since) if sender else None
        result = answer(conn, site, question, business, history=history)
        if sender:
            sessions.touch(conn, conv["id"], question)
            conn.execute(
                "INSERT INTO wa_answers (site, sender, question, answerable, "
                "latency_ms) VALUES (%s,%s,%s,%s,%s)",
                (site, sender, question, bool(result.get("answerable")),
                 int(float(result.get("latency") or 0) * 1000)))
            save_turn(conn, site, sender, "customer", question)
            save_turn(conn, site, sender, "you", result.get("reply", ""))
            reason = ("masked_price" if result.get("masked_price_blocked")
                      else "not answerable from site")
            if not result.get("answerable"):
                conn.execute(
                    "INSERT INTO escalations (site, sender, question, "
                    "bot_reply, reason, conversation_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (site, sender, question, result.get("reply", ""), reason,
                     conv["id"]))
                sessions.escalate(conn, conv["id"],
                                  result.get("reply", ""), reason)
                result["escalated"] = True
                # A promise with a time on it is one we can keep. Silence at
                # 11pm is not.
                if not upkeep.is_open():
                    from query import detect_language
                    back = upkeep.when_back(detect_language(question))
                    if back:
                        result["reply"] = result.get("reply", "") + back
            else:
                sessions.answered(conn, conv["id"], result.get("reply", ""))
        result["history_turns"] = len(history or [])
        return result


# ------------------------------------------------------------ whatsapp verify
@app.get("/webhook/whatsapp")
def verify(request: Request) -> Response:
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == WA_VERIFY:
        jlog("webhook_verified")
        return Response(content=q.get("hub.challenge", ""), media_type="text/plain")
    jlog("webhook_verify_failed", mode=q.get("hub.mode"))
    return Response(status_code=403, content="verification failed")


def signature_ok(raw: bytes, header: str | None) -> bool:
    """HMAC-SHA256 over the RAW body.

    It must be the exact bytes Meta sent — re-serialising the parsed dict
    changes key order and whitespace and will never match.
    """
    if not WA_SECRET or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(WA_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


@app.post("/webhook/whatsapp")
async def incoming(request: Request, background: BackgroundTasks,
                   x_hub_signature_256: str | None = Header(default=None)) -> Response:
    raw = await request.body()
    if not signature_ok(raw, x_hub_signature_256):
        jlog("bad_signature")
        return Response(status_code=403, content="bad signature")

    try:
        payload = json.loads(raw)
    except ValueError:
        return Response(status_code=200, content="ok")

    background.add_task(handle_payload, payload)
    # 200 first, work afterwards. Anything slower and Meta retries.
    return Response(status_code=200, content="ok")


# ----------------------------------------------------------------- handling
async def handle_payload(payload: dict) -> None:
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            # Meta names the receiving number here; it is the only thing that
            # says which clinic this message belongs to.
            tenant = tenant_for(str(value.get("metadata", {})
                                    .get("phone_number_id", "")))
            for msg in value.get("messages", []):
                try:
                    await handle_message(msg, tenant)
                except Exception as exc:
                    jlog("handler_error", error=str(exc)[:300])


async def fetch_media(media_id: str, tenant: Tenant) -> tuple[bytes, str]:
    """Two hops: Meta gives a short-lived URL, then the bytes. Both need the
    clinic's own token, so this cannot be shared between tenants."""
    headers = {"Authorization": f"Bearer {tenant.token}"}
    async with httpx.AsyncClient(timeout=60) as client:
        meta = await client.get(f"{GRAPH_ROOT}/{media_id}", headers=headers)
        meta.raise_for_status()
        info = meta.json()
        blob = await client.get(info["url"], headers=headers)
        blob.raise_for_status()
    return blob.content, info.get("mime_type", "audio/ogg")


async def voice_to_text(msg: dict, kind: str, sender: str,
                        tenant: Tenant) -> str | None:
    """Transcript of a voice note, or None once the customer has been told."""
    media_id = (msg.get(kind) or {}).get("id")
    if not media_id:
        return None
    try:
        audio, mime = await fetch_media(media_id, tenant)
        text = await asyncio.to_thread(transcribe, audio, "voice.ogg", mime)
    except Exception as exc:
        jlog("transcribe_failed", sender=sender, site=tenant.site,
             error=str(exc)[:300])
        await send(sender, replies.line("voice_unclear"), tenant)
        return None
    if not text:
        await send(sender, replies.line("voice_empty"), tenant)
        return None
    jlog("transcribed", sender=sender, site=tenant.site, seconds=None,
         bytes=len(audio), text=text)
    return text


async def handle_message(msg: dict, tenant: Tenant) -> None:
    msg_id, sender, kind = msg.get("id", ""), msg.get("from", ""), msg.get("type")
    if not msg_id or await asyncio.to_thread(already_seen, msg_id):
        jlog("duplicate_ignored", msg_id=msg_id)
        return

    if kind in ("audio", "voice"):
        text = await voice_to_text(msg, kind, sender, tenant)
        if not text:
            return                      # the customer already has an answer
    elif kind == "text":
        text = (msg.get("text") or {}).get("body", "").strip()
    else:
        # Images and documents still need a person: reading an x-ray or a
        # receipt is not something this bot should pretend to do.
        jlog("non_text", msg_id=msg_id, kind=kind, sender=sender,
             site=tenant.site)
        await send(sender, replies.line("media_unsupported"), tenant)
        return

    if not text:
        return
    jlog("received", msg_id=msg_id, sender=sender, site=tenant.site, text=text)

    conv = await asyncio.to_thread(session_state, tenant.site, sender)
    if conv["status"] == "human":
        # A person is mid-conversation with them. Record it so the thread
        # stays complete, but do not answer over the top of a colleague.
        await asyncio.to_thread(record_incoming, tenant.site, sender, text)
        if await asyncio.to_thread(claim_ack, conv["id"]):
            lang = detect_language(text)
            ack = replies.line("person_has_it", lang)
            await send(sender, ack, tenant)
            await asyncio.to_thread(record_outgoing, tenant.site, sender, ack)
            jlog("handover_ack", sender=sender, site=tenant.site, lang=lang)
        else:
            jlog("paused_no_reply", sender=sender, site=tenant.site)
        return

    # Keyed by clinic as well as sender, so one person messaging two of our
    # clients does not have their bubbles merged into a single question.
    key = f"{tenant.phone_id}:{sender}"
    _pending.setdefault(key, []).append(text)
    if old := _timers.get(key):
        old.cancel()
    _timers[key] = asyncio.create_task(flush_after_debounce(key, sender, tenant))


async def flush_after_debounce(key: str, sender: str, tenant: Tenant) -> None:
    try:
        await asyncio.sleep(DEBOUNCE_S)
    except asyncio.CancelledError:
        return          # another bubble arrived; that task will flush instead
    parts = _pending.pop(key, [])
    _timers.pop(key, None)
    if not parts:
        return

    question = " ".join(parts)
    started = time.time()
    try:
        result = await asyncio.to_thread(
            _answer_sync, tenant.site, question, tenant.business or None,
            sender)
        reply = result.get("reply") or replies.line("handing_over")
    except Exception as exc:
        jlog("answer_failed", sender=sender, error=str(exc)[:300])
        # Never go silent mid-conversation.
        from query import detect_language
        reply = replies.line("trouble", detect_language(question))
        # And never make that promise without keeping it. This path fires on
        # exactly the outages a customer must not be stranded by — an
        # exhausted API quota takes out embedding and generation together, so
        # every message would otherwise get an apology that reached nobody.
        result = {"escalated": True, "failure": str(exc)[:200]}

    jlog("answered", sender=sender, site=tenant.site, bubbles=len(parts),
         question=question,
         answerable=result.get("answerable"), top_score=result.get("top_score"),
         history_turns=result.get("history_turns"),
         escalated=bool(result.get("escalated")),
         latency=round(time.time() - started, 2),
         chunk_urls=result.get("chunk_urls", []))
    await send(sender, reply, tenant)

    if result.get("escalated"):
        await notify_human(sender, question, reply, tenant,
                           result.get("failure"))


async def notify_human(sender: str, question: str, reply: str,
                       tenant: Tenant | None = None,
                       failure: str | None = None) -> None:
    """Tell a human a customer is waiting. Otherwise 'I'll check with my
    colleague' is a promise nobody keeps."""
    t = tenant or DEFAULT_TENANT
    if not t.escalation:
        jlog("escalation_logged_only", sender=sender, site=t.site,
             question=question)
        return
    # Testing with your own number as the escalation contact means the
    # customer gets their own alert — two messages for one enquiry, both
    # billable from 1 Oct 2026. The dashboard still shows the case.
    if t.escalation.lstrip("+") == sender.lstrip("+"):
        jlog("escalation_self_skipped", sender=sender, site=t.site)
        return
    # An outage reads very differently from "the site does not say" — one
    # needs someone to answer the customer, the other needs someone to fix
    # the bot. Say which.
    why = f"\n\nBOT IS DOWN: {failure}" if failure else ""
    await send(t.escalation,
               f"[{t.business or t.site}] +{sender} asked:\n\"{question}\"\n\n"
               f"Bot replied: {reply[:200]}{why}\n\nThey are waiting for a human.", t)
    jlog("escalation_notified", to=t.escalation, site=t.site, sender=sender)


async def send(to: str, body: str, tenant: Tenant | None = None) -> bool:
    """Exactly one message. Splitting costs a multiple of the send fee.

    Returns whether Meta accepted it. Callers that tell a human "sent" must
    check: a silent failure here means someone believes a customer was
    answered when they were not.
    """
    t = tenant or DEFAULT_TENANT
    if not (t.token and t.phone_id):
        jlog("send_skipped_unconfigured", to=to, site=t.site, body=body[:120])
        return False
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            t.graph,
            headers={"Authorization": f"Bearer {t.token}"},
            json={"messaging_product": "whatsapp", "to": to,
                  "type": "text", "text": {"body": body[:4000]}},
        )
    jlog("sent", to=to, site=t.site, status=r.status_code,
         error=None if r.is_success else r.text[:300])
    return r.is_success
