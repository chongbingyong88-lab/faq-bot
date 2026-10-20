"""Re-grade stored replies with a different judge. No bot calls.

    python evals/rejudge.py evals/results/sepadental-<stamp>.json --judge gemini-3.5-flash-lite

Only the semantic criteria go to a judge; `required` / `forbidden` are string
checks and are not re-run. So this measures one thing: how much of a run's
failure count is the judge rather than the bot.

Needed because a judge is a model too. Pinning it (JUDGE_MODEL) stops it
drifting with the bot, but pinning it to a *different* model than an earlier
run makes that run's number incomparable — which is exactly what happened
between the 78%/77% baseline and the first pinned run.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from ingest.llm import generate  # noqa: E402

HERE = Path(__file__).resolve().parent
from run import JUDGE_SYSTEM  # noqa: E402,E501  (same rubric, same wording)


def judge_with(model: str, question: str, criterion: str, reply: str) -> tuple[bool, str]:
    out = generate(JUDGE_SYSTEM,
                   f"CRITERION:\n{criterion}\n\n"
                   f"CUSTOMER QUESTION:\n{question}\n\nREPLY:\n{reply}",
                   model=model)
    lines = out.strip().splitlines()
    first = lines[0].strip().upper() if lines else ""
    return first.startswith("PASS"), " ".join(lines[1:])[:160]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--judge", required=True, help="model id to grade with")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    data = json.loads(Path(args.results).read_text(encoding="utf-8"))
    site = data["site"]
    spec = {r["question"]: r for r in
            yaml.safe_load((HERE / f"{site}.yaml").read_text(encoding="utf-8"))}

    # Only rows whose ONLY complaint was the judge can be overturned by one.
    targets = [r for r in data["rows"]
               if r["passed"] is False
               and all(f.startswith("judge:") for f in r["fails"])]
    print(f"{site}: re-judging {len(targets)} judge-only failures "
          f"with {args.judge}\n  (was {data.get('judge_model')})\n")

    flipped = []
    for r in targets:
        criterion = ((spec[r["question"]].get("evaluation") or {}).get("semantic"))
        if not criterion:
            continue
        try:
            ok, why = judge_with(args.judge, r["question"], criterion, r["reply"])
        except Exception as exc:
            print(f"  SKIP  {r['question'][:40]}  ({str(exc)[:50]})")
            continue
        if ok:
            flipped.append(r)
        print(f"  {'PASS (was FAIL)' if ok else 'FAIL (agreed)':<16} "
              f"{r['lang']:<6} {r['question'][:44]}")
        if ok:
            print(f"        old judge: {r['fails'][0][:110]}")

    acc = data["accuracy"]
    gained = sum(1 for r in flipped if r["answerable"])
    print(f"\n  {len(flipped)}/{len(targets)} overturned by the new judge")
    print(f"  accuracy on answerable: {acc['passed']}/{acc['total']} -> "
          f"{acc['passed'] + gained}/{acc['total']}"
          f"  ({100*(acc['passed']+gained)/acc['total']:.0f}%)")
    print("\n  A disagreement is not proof the new judge is right — read the "
          "replies above and decide which grading you trust.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
