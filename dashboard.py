"""The screen a clinic runs the day from.

Mounted by app.py at /dashboard, ordered by what needs a person soonest:

  1. Who you are already handling, and until when.
  2. Who is waiting after being promised a callback.
  3. What the bot is dealing with on its own.
  4. What it has been telling people, and how much it handled unaided.

The queue comes from `conversations` — the state machine — never from the
escalation log. The log records that a handover happened; the conversation
records whether anyone has since dealt with it. Reading the log produced a
queue that still listed customers who had already been answered.

Kept out of app.py because it is presentation: the webhook must stay
readable, and nothing here may ever block answering a customer.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone

import sessions
import upkeep

STATS_SQL = """
SELECT count(*)                            AS total,
       count(*) FILTER (WHERE answerable)  AS handled,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS median_ms
FROM wa_answers
WHERE at > now() - make_interval(days => %s) AND (%s::text IS NULL OR site = %s)
"""

DAILY_SQL = """
SELECT date_trunc('day', at AT TIME ZONE 'Asia/Kuala_Lumpur')::date AS day,
       count(*)                    AS total,
       count(*) FILTER (WHERE answerable) AS handled
FROM wa_answers
WHERE at > now() - make_interval(days => %s) AND (%s::text IS NULL OR site = %s)
GROUP BY 1 ORDER BY 1
"""

TOP_SQL = """
SELECT question, count(*) AS n
FROM wa_answers
WHERE at > now() - make_interval(days => %s) AND (%s::text IS NULL OR site = %s)
GROUP BY 1 ORDER BY n DESC, question LIMIT 8
"""

RECENT_SQL = """
SELECT sender, question, answerable, latency_ms, at
FROM wa_answers
WHERE at > now() - make_interval(days => %s) AND (%s::text IS NULL OR site = %s)
ORDER BY at DESC LIMIT 25
"""

THREAD_SQL = """
SELECT site, sender, role, text FROM wa_history
WHERE sender = ANY(%s) AND (%s::text IS NULL OR site = %s)
  AND at > now() - interval '2 days'
ORDER BY at
"""


def _shape(c: dict) -> dict:
    return {
        "id": c["id"], "site": c["site"], "sender": c["sender"],
        "question": c["last_question"] or "",
        "reply": c["last_reply"] or "",
        "messages": c["messages"],
        "at": (c["escalated_at"] or c["last_at"]).isoformat(),
        "until": c["hold_until"].isoformat() if c["hold_until"] else "",
        # Minutes of free-form reply left under WhatsApp's 24-hour rule.
        "window": sessions.window_left_minutes(c),
    }


def collect(conn, site: str | None, days: int) -> dict:
    a = (days, site, site)
    total, handled, median_ms = conn.execute(STATS_SQL, a).fetchone()
    board = sessions.board(conn, site)
    waiting = [_shape(c) for c in board["waiting"]]
    holding = [_shape(c) for c in board["human"]]
    with_bot = [_shape(c) for c in board["bot"]]

    senders = list({c["sender"] for c in waiting + holding + with_bot})
    threads: dict[str, list[dict]] = {}
    if senders:
        for thread_site, snd, role, text in conn.execute(
                THREAD_SQL, (senders, site, site)).fetchall():
            threads.setdefault((thread_site, snd), []).append({"role": role, "text": text})

    return {
        "waiting": waiting, "holding": holding, "with_bot": with_bot,
        "threads": threads,
        "total": total or 0,
        "handled": handled or 0,
        "median_ms": int(median_ms) if median_ms is not None else None,
        "daily": [{"day": d.isoformat(), "total": t, "handled": h}
                  for d, t, h in conn.execute(DAILY_SQL, a).fetchall()],
        "top": [{"question": q, "n": n}
                for q, n in conn.execute(TOP_SQL, a).fetchall()],
        "recent": [{"sender": r[0], "question": r[1], "answerable": r[2],
                    "latency_ms": r[3], "at": r[4].isoformat()}
                   for r in conn.execute(RECENT_SQL, (days, site, site)).fetchall()],
    }


# ------------------------------------------------------------------ helpers
def _mins(iso: str) -> int:
    return int((datetime.now(timezone.utc)
                - datetime.fromisoformat(iso)).total_seconds() // 60)


def _ago(iso: str) -> str:
    m = _mins(iso)
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m}m ago"
    if m < 1440:
        return f"{m // 60}h ago"
    return f"{m // 1440}d ago"


def _urgency(iso: str) -> str:
    """Someone waiting twenty minutes is a different problem from someone
    waiting two, and the card should say so before it is read."""
    m = _mins(iso)
    return "late" if m >= 15 else ("soon" if m >= 5 else "")


def _bars(daily: list[dict]) -> str:
    if not daily:
        return '<p class="empty">No questions in this period.</p>'
    peak = max(d["total"] for d in daily) or 1
    out = []
    for d in daily:
        esc = d["total"] - d["handled"]
        out.append(
            f'<div class="bar" title="{d["day"]}: {d["total"]} asked, '
            f'{esc} needed a person">'
            f'<div class="esc" style="height:{round(100 * esc / peak, 1)}%"></div>'
            f'<div class="ok" style="height:{round(100 * d["handled"] / peak, 1)}%"></div>'
            f'<span>{d["day"][-2:]}</span></div>')
    return f'<div class="bars">{"".join(out)}</div>'


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _thread(turns: list[dict], e) -> str:
    if not turns:
        return ""
    body = "".join(
        f'<div class="turn {"cust" if t["role"] == "customer" else "bot"}">'
        f'<span>{"Customer" if t["role"] == "customer" else "Bot"}</span>'
        f'{e(t["text"][:400])}</div>' for t in turns[-8:])
    return (f'<details class="thread"><summary>Show conversation '
            f'({_plural(len(turns), "message")})</summary>{body}</details>')


from desk_ui import render, login_page

