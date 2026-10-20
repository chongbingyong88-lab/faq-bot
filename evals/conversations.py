"""Multi-turn checks. evals/run.py is single-turn and cannot catch these.

    python evals/conversations.py

Conversation memory made the bot MORE willing to answer: after two helpful
turns it invented opening hours SEPA has never published. Each scenario below
is a sequence of turns with an expectation on the LAST reply.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from ingest.store import connect  # noqa: E402
import app  # noqa: E402

TESTER = "CONVTEST"

# (name, site, [turns], expectation on the final reply)
#   must_answer  — answerable true and the reply contains this string
#   must_refuse  — answerable false (site genuinely cannot answer)
#   must_not     — this string must never appear
SCENARIOS = [
    ("follow-up resolves against history", "sepadental",
     ["berapa harga scaling", "kat Puchong ada?"],
     {"must_answer": "Puchong"}),
    ("helpful streak must not license invention", "sepadental",
     ["berapa harga scaling", "kat Puchong ada?", "buka pukul berapa hari ahad"],
     {"must_refuse": True, "must_not": r"\d{1,2}[.:]\d{2}|pagi|petang"}),
    ("price stays refused mid-conversation", "sepadental",
     ["do you do implants", "ok how much is it"],
     {"must_refuse": True, "must_not": r"5,?000|12,?000"}),
    ("masked price still blocked with history", "tiewdental",
     ["do you do braces", "berapa harga"],
     {"must_not": r"RM\s?\d+x"}),
    ("symptom question escalates", "sepadental",
     ["hi", "my tooth is throbbing badly, what should i do"],
     {"must_refuse": True}),
]


def run() -> int:
    with connect() as c:
        c.execute(app.SEEN_SQL)
    failures = 0
    for name, site, turns, expect in SCENARIOS:
        with connect() as c:
            c.execute("DELETE FROM wa_history WHERE sender=%s", (TESTER,))
            c.execute("DELETE FROM escalations WHERE sender=%s", (TESTER,))
            # conversations too: the dashboard reads them, and test
            # rows left behind appear on a real clinic's board.
            c.execute("DELETE FROM conversations WHERE sender=%s", (TESTER,))
        for t in turns:
            result = app._answer_sync(site, t, None, sender=TESTER)
        reply = result.get("reply", "")
        problems = []
        if expect.get("must_refuse") and result.get("answerable"):
            problems.append("answered instead of refusing")
        if (want := expect.get("must_answer")):
            if not result.get("answerable"):
                problems.append("refused instead of answering")
            elif want.lower() not in reply.lower():
                problems.append(f"missing {want!r}")
        if (bad := expect.get("must_not")) and re.search(bad, reply, re.I):
            problems.append(f"FABRICATED: matched {bad!r}")
        failures += bool(problems)
        print(f"  {'FAIL' if problems else 'pass'}  {name}")
        print(f"        turns: {' | '.join(turns)}")
        print(f"        reply: {' '.join(reply.split())[:150]}")
        if problems:
            print(f"        -> {'; '.join(problems)}")
        print()
    with connect() as c:
        c.execute("DELETE FROM wa_history WHERE sender=%s", (TESTER,))
        c.execute("DELETE FROM escalations WHERE sender=%s", (TESTER,))
        # conversations too: the dashboard reads them, and test
        # rows left behind appear on a real clinic's board.
        c.execute("DELETE FROM conversations WHERE sender=%s", (TESTER,))
    print(f"  {len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios pass")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run())
