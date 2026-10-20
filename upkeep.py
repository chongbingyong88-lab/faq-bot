"""Business hours, waiting-customer nudges, and data retention.

Three jobs that all answer the same question — what do we owe someone we are
not currently talking to?

  hours      "our team is offline until 9am" is a promise we can keep;
             silence at 11pm is not.
  nudges     a customer told a colleague would come back, and none has, is
             owed an update rather than more silence.
  retention  a clinic is a data controller. Keeping a patient's phone number
             beside "my tooth is throbbing" forever is a liability, and the
             reason to delete is the PDPA, not disk space — a clinic doing a
             hundred messages a day generates about 30 MB a year.
"""
from __future__ import annotations

import os
from datetime import datetime, time, timedelta, timezone

# Malaysia has no daylight saving, so a fixed offset is honest here. Anywhere
# that does would need a real timezone database.
TZ_OFFSET_HOURS = float(os.getenv("CLINIC_UTC_OFFSET", "8"))
TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))
# "09:00-18:00", or empty to treat the clinic as always staffed.
HOURS = os.getenv("BUSINESS_HOURS", "09:00-18:00").strip()
# 0 = Monday. Default Monday-Saturday, which is how most clinics here run.
OPEN_DAYS = {int(d) for d in os.getenv("BUSINESS_DAYS", "0,1,2,3,4,5").split(",")
             if d.strip().isdigit()}

RETAIN = {
    # Only guards against Meta redelivering a message; useless after days.
    "wa_seen": int(os.getenv("RETAIN_SEEN_DAYS", "7")),
    # Conversation transcripts — the most sensitive thing stored.
    "wa_history": int(os.getenv("RETAIN_HISTORY_DAYS", "90")),
    # The handover log.
    "escalations": int(os.getenv("RETAIN_ESCALATIONS_DAYS", "90")),
    # Closed conversations.
    "conversations": int(os.getenv("RETAIN_CONVERSATIONS_DAYS", "90")),
    # Question TEXT is redacted at this age; the row stays so the dashboard
    # keeps its counts. Statistics are not personal data once the words are
    # gone, and a clinic should not lose a year of "how much did the bot
    # handle" to a privacy policy.
    "wa_answers_text": int(os.getenv("RETAIN_QUESTION_TEXT_DAYS", "90")),
}


# ------------------------------------------------------------ opening hours
def _window() -> tuple[time, time] | None:
    if "-" not in HOURS:
        return None
    a, _, b = HOURS.partition("-")
    try:
        return (time.fromisoformat(a.strip()), time.fromisoformat(b.strip()))
    except ValueError:
        return None


def is_open(now: datetime | None = None) -> bool:
    w = _window()
    if w is None:
        return True
    now = (now or datetime.now(timezone.utc)).astimezone(TZ)
    return now.weekday() in OPEN_DAYS and w[0] <= now.time() < w[1]


def opens_next(now: datetime | None = None) -> datetime | None:
    """When someone will next be at the desk."""
    w = _window()
    if w is None:
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(TZ)
    day = now
    for step in range(8):
        if day.weekday() in OPEN_DAYS:
            start = day.replace(hour=w[0].hour, minute=w[0].minute,
                                second=0, microsecond=0)
            if start > now:
                return start
            if step == 0 and now.time() < w[1]:
                return None            # open right now
        day = (day + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
    return None


def when_back(lang: str | None = None) -> str:
    """A phrase for the customer, in their language, or "" if open now."""
    nxt = opens_next()
    if nxt is None:
        return ""
    today = datetime.now(TZ).date()
    when = ("later today" if nxt.date() == today else
            "tomorrow" if nxt.date() == today + timedelta(days=1) else
            nxt.strftime("on %A"))
    h12 = nxt.hour % 12 or 12
    if lang == "Malay":
        # "9am" inside a Malay sentence reads as an unfinished translation.
        part = ("pagi" if nxt.hour < 12 else
                "petang" if nxt.hour < 19 else "malam")
        ms = {"later today": "kemudian hari ini", "tomorrow": "esok"}
        return (f" Pasukan kami akan kembali {ms.get(when, when)} "
                f"pada pukul {h12} {part}.")
    if lang == "Chinese":
        part = "上午" if nxt.hour < 12 else "下午"
        zh = {"later today": "今天稍后", "tomorrow": "明天"}
        return f" 我们的团队{zh.get(when, when)}{part}{h12}点回来。"
    ampm = "am" if nxt.hour < 12 else "pm"
    return f" Our team is back {when} at {h12}{ampm}."


# ------------------------------------------------------------------ nudging
def nudge_text(lang: str | None = None) -> str:
    """Wording lives in replies.py with every other line the bot says for
    itself, so the voice stays one voice."""
    import replies
    return replies.line("still_with_us", lang)


# --------------------------------------------------------------- retention
def purge(conn) -> dict[str, int]:
    """Delete what we have said we do not keep. Returns what went."""
    gone: dict[str, int] = {}
    for table, days in (("wa_seen", RETAIN["wa_seen"]),
                        ("wa_history", RETAIN["wa_history"]),
                        ("escalations", RETAIN["escalations"])):
        col = "seen_at" if table == "wa_seen" else "at"
        gone[table] = conn.execute(
            f"DELETE FROM {table} WHERE {col} < now() - "
            f"make_interval(days => %s)", (days,)).rowcount
    gone["conversations"] = conn.execute(
        "DELETE FROM conversations WHERE status='closed' "
        "AND closed_at < now() - make_interval(days => %s)",
        (RETAIN["conversations"],)).rowcount
    # Redact rather than delete: the counts behind the dashboard survive, the
    # customer's words do not.
    gone["wa_answers redacted"] = conn.execute(
        "UPDATE wa_answers SET question='(redacted)', sender='(redacted)' "
        "WHERE question <> '(redacted)' "
        "AND at < now() - make_interval(days => %s)",
        (RETAIN["wa_answers_text"],)).rowcount
    return {k: v for k, v in gone.items() if v}


def forget(conn, site: str, sender: str) -> dict[str, int]:
    """Erase one customer on request — the PDPA answer, and the one a client
    will ask for before signing anything."""
    gone = {}
    for table in ("wa_history", "escalations", "conversations"):
        gone[table] = conn.execute(
            f"DELETE FROM {table} WHERE site=%s AND sender=%s",
            (site, sender)).rowcount
    gone["wa_answers redacted"] = conn.execute(
        "UPDATE wa_answers SET question='(redacted)', sender='(redacted)' "
        "WHERE site=%s AND sender=%s", (site, sender)).rowcount
    return {k: v for k, v in gone.items() if v}


def policy() -> str:
    """One sentence for a client, and for the dashboard footer."""
    return (f"Messages are kept for {RETAIN['wa_history']} days, then "
            f"deleted. Counts are kept without the wording.")
