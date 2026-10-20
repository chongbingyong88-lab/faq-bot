"""One conversation per customer per clinic, with an explicit state.

Before this, "what is happening with this customer" had to be inferred by
joining three tables and guessing: history rows inside a rolling 30-minute
window, a pause row that may or may not have expired, an escalation row that
may or may not have been ticked. Nothing recorded that a conversation had
begun, ended, or been handed between the bot and a person.

    bot ──escalates──> waiting ──someone replies──> human
     ^                    │                           │
     └────────────────────┴──gives back / hold expires┘
     │
     └──> closed   (idle too long, or marked done)

Rules the states encode:

  bot      the bot is answering. Normal.
  waiting  the bot said a colleague would come back, and none has. This is
           the queue, and it is the only state with a promise outstanding.
  human    a person is replying. The bot stays quiet, with a deadline so
           forgetting to hand back cannot strand anyone.
  closed   finished, or idle long enough that the next message is a new
           conversation rather than a continuation.

Every transition goes through this module. Statuses that can be set from
several places drift apart otherwise.
"""
from __future__ import annotations

import os

# Two different clocks, previously one, which was wrong in both directions.
#
# CONTEXT_MINUTES is how far back the model may look. Short on purpose: stale
# context is what made it invent opening hours after a few helpful turns.
#
# SESSION_HOURS is how long the case stays open. Long on purpose: WhatsApp is
# asynchronous. Someone replying three hours later believes they are still in
# the same conversation, and closing at 60 minutes threw those away.
CONTEXT_MINUTES = int(os.getenv("CONTEXT_MINUTES", "30"))
SESSION_HOURS = int(os.getenv("SESSION_HOURS", "24"))
# How long the bot stays quiet after a person replies.
HOLD_MINUTES = int(os.getenv("HANDOVER_MINUTES", "15"))
# WhatsApp allows free-form replies for 24 hours after the customer's last
# message. Outside it Meta rejects anything but an approved template, so this
# is a real deadline on the operator, not a policy of ours.
SERVICE_WINDOW_HOURS = 24
# Waiting this long with nobody replying, and the customer is told so rather
# than left wondering.
NUDGE_AFTER_MINUTES = int(os.getenv("NUDGE_AFTER_MINUTES", "30"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id           BIGSERIAL PRIMARY KEY,
    site         TEXT NOT NULL,
    sender       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'bot',
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at    TIMESTAMPTZ,
    hold_until   TIMESTAMPTZ,
    acked        BOOLEAN NOT NULL DEFAULT false,
    -- When the customer last wrote. WhatsApp's 24-hour service window runs
    -- from here, and it decides whether a staff reply can be delivered.
    last_customer_at TIMESTAMPTZ,
    nudged_at    TIMESTAMPTZ,
    messages     INTEGER NOT NULL DEFAULT 0,
    escalated_at TIMESTAMPTZ,
    reason       TEXT,
    last_question TEXT,
    last_reply    TEXT
);
-- At most one live conversation per customer per clinic. Enforced by the
-- database rather than by remembering to check.
CREATE UNIQUE INDEX IF NOT EXISTS conversations_one_open
    ON conversations (site, sender) WHERE status <> 'closed';
CREATE INDEX IF NOT EXISTS conversations_queue
    ON conversations (site, status, last_at DESC);
-- CREATE TABLE IF NOT EXISTS does nothing to a table that already exists,
-- so new columns need saying twice.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS last_customer_at TIMESTAMPTZ;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS nudged_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS conversations_nudge
    ON conversations (status, escalated_at) WHERE nudged_at IS NULL;
"""

OPEN_STATUSES = ("bot", "waiting", "human")


def _row(r) -> dict | None:
    if r is None:
        return None
    keys = ("id", "site", "sender", "status", "started_at", "last_at",
            "hold_until", "acked", "messages", "escalated_at", "reason",
            "last_question", "last_reply", "last_customer_at", "nudged_at")
    return dict(zip(keys, r))

SELECT_COLS = ("id, site, sender, status, started_at, last_at, hold_until, "
               "acked, messages, escalated_at, reason, last_question, "
               "last_reply, last_customer_at, nudged_at")


def begin(conn, site: str, sender: str) -> dict:
    """The conversation this message belongs to, creating one if needed.

    Called on every inbound message, and the only place that resolves the
    two time-based transitions — an expired hold and an idle conversation.
    Doing it lazily here means no background sweeper to forget to run, and
    no window where the stored status is a lie.
    """
    # An expired hold is the bot's turn again.
    conn.execute(
        "UPDATE conversations SET status='bot', hold_until=NULL "
        "WHERE site=%s AND sender=%s AND status='human' AND hold_until < now()",
        (site, sender))
    # Idle beyond the session's life: close it so this message starts fresh.
    conn.execute(
        "UPDATE conversations SET status='closed', closed_at=now() "
        "WHERE site=%s AND sender=%s AND status <> 'closed' "
        "AND last_at < now() - make_interval(hours => %s)",
        (site, sender, SESSION_HOURS))

    row = conn.execute(
        f"SELECT {SELECT_COLS} FROM conversations "
        "WHERE site=%s AND sender=%s AND status <> 'closed'",
        (site, sender)).fetchone()
    if row is None:
        row = conn.execute(
            f"INSERT INTO conversations (site, sender) VALUES (%s,%s) "
            f"RETURNING {SELECT_COLS}", (site, sender)).fetchone()
    return _row(row)


def touch(conn, conv_id: int, question: str) -> None:
    """A message from the customer. Restarts WhatsApp's 24-hour window."""
    conn.execute(
        "UPDATE conversations SET last_at=now(), messages=messages+1, "
        "last_customer_at=now(), nudged_at=NULL, last_question=%s "
        "WHERE id=%s", (question[:500], conv_id))


def window_left_minutes(conv: dict) -> int | None:
    """Minutes of free-form reply left, or None if it was never opened."""
    from datetime import datetime, timedelta, timezone
    seen = conv.get("last_customer_at")
    if seen is None:
        return None
    closes = seen + timedelta(hours=SERVICE_WINDOW_HOURS)
    return int((closes - datetime.now(timezone.utc)).total_seconds() // 60)


def can_reply_freely(conv: dict) -> bool:
    left = window_left_minutes(conv)
    return left is None or left > 0


def get(conn, site: str, sender: str) -> dict | None:
    return _row(conn.execute(
        f"SELECT {SELECT_COLS} FROM conversations "
        "WHERE site=%s AND sender=%s AND status <> 'closed'",
        (site, sender)).fetchone())


def needing_nudge(conn) -> list[dict]:
    """Waiting too long, nobody has replied, and still repliable.

    Nudging after the service window shuts would be a message Meta refuses
    to deliver — an apology the customer never sees, billed anyway.
    """
    rows = conn.execute(
        f"SELECT {SELECT_COLS} FROM conversations "
        "WHERE status='waiting' AND nudged_at IS NULL "
        "AND escalated_at < now() - make_interval(mins => %s) "
        "AND last_customer_at > now() - make_interval(hours => %s) "
        "ORDER BY escalated_at LIMIT 20",
        (NUDGE_AFTER_MINUTES, SERVICE_WINDOW_HOURS)).fetchall()
    return [_row(r) for r in rows]


def mark_nudged(conn, conv_id: int) -> bool:
    cur = conn.execute(
        "UPDATE conversations SET nudged_at=now() "
        "WHERE id=%s AND nudged_at IS NULL", (conv_id,))
    return cur.rowcount == 1


def answered(conn, conv_id: int, reply: str) -> None:
    """The bot replied and nothing is outstanding."""
    conn.execute(
        "UPDATE conversations SET last_reply=%s, last_at=now(), "
        "status = CASE WHEN status='waiting' THEN 'bot' ELSE status END, "
        "escalated_at = CASE WHEN status='waiting' THEN NULL "
        "                    ELSE escalated_at END "
        "WHERE id=%s", (reply[:500], conv_id))


def escalate(conn, conv_id: int, reply: str, reason: str) -> None:
    """A promise is now outstanding. Never downgrade a human takeover to it."""
    conn.execute(
        "UPDATE conversations SET status='waiting', escalated_at=now(), "
        "reason=%s, last_reply=%s, last_at=now() "
        "WHERE id=%s AND status <> 'human'", (reason, reply[:500], conv_id))


def take_over(conn, site: str, sender: str, reply: str) -> None:
    """A person replied. Hold the bot, and re-acknowledge only if this is a
    genuinely new takeover rather than a continuing one."""
    conn.execute(
        "UPDATE conversations SET status='human', last_reply=%s, last_at=now(), "
        "hold_until = now() + make_interval(mins => %s), "
        "acked = CASE WHEN status='human' THEN acked ELSE false END "
        "WHERE site=%s AND sender=%s AND status <> 'closed'",
        (reply[:500], HOLD_MINUTES, site, sender))


def hand_back(conn, site: str, sender: str) -> None:
    conn.execute(
        "UPDATE conversations SET status='bot', hold_until=NULL "
        "WHERE site=%s AND sender=%s AND status='human'", (site, sender))


def close(conn, site: str, sender: str) -> None:
    conn.execute(
        "UPDATE conversations SET status='closed', closed_at=now() "
        "WHERE site=%s AND sender=%s AND status <> 'closed'", (site, sender))


def claim_ack(conn, conv_id: int) -> bool:
    """True exactly once per takeover — a single conditional update, so two
    bubbles arriving together cannot both send an acknowledgement."""
    cur = conn.execute(
        "UPDATE conversations SET acked=true "
        "WHERE id=%s AND status='human' AND NOT acked", (conv_id,))
    return cur.rowcount == 1


def board(conn, site: str | None) -> dict[str, list[dict]]:
    """Everything live, grouped by what it needs from a person."""
    rows = conn.execute(
        f"SELECT {SELECT_COLS} FROM conversations "
        "WHERE status <> 'closed' AND (%s::text IS NULL OR site = %s) "
        "ORDER BY last_at DESC LIMIT 100", (site, site)).fetchall()
    out: dict[str, list[dict]] = {s: [] for s in OPEN_STATUSES}
    for r in rows:
        d = _row(r)
        out.setdefault(d["status"], []).append(d)
    return out
