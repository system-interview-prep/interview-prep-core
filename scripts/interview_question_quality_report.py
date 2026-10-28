"""Print quality signals of the interview questions actually asked, per stage and source.

Answers are graded elsewhere; this judges the questions: answered and skip
rates, score spread, follow-up and re-explanation rates (and how many
follow-ups the model wrote), time-box overruns, planned-difficulty fit,
repeats, wrong language and grading-word leaks. No model calls.

Usage:
    python scripts/interview_question_quality_report.py
    python scripts/interview_question_quality_report.py --no-demo --exclude-email "e2e.%" --json out.json
    python scripts/interview_question_quality_report.py --session <session-id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.infrastructure.database import SessionFactory  # noqa: E402
from src.modules.interviews.evaluation.question_quality import question_quality_report  # noqa: E402

_COLUMNS = (
    ("stage", "stage", 11, "{}"),
    ("source", "source", 23, "{}"),
    ("asked", "asked", 6, "{}"),
    ("answeredRate", "answered", 9, "{:.0%}"),
    ("meanScore", "score", 6, "{:.1f}"),
    ("scoreStdDev", "sd", 5, "{:.1f}"),
    ("probesPerTurn", "probe/q", 8, "{:.2f}"),
    ("llmProbeShare", "llm-probe", 10, "{:.0%}"),
    ("clarifiesPerTurn", "clarify/q", 10, "{:.2f}"),
    ("overHardTimeRate", "overtime", 9, "{:.0%}"),
    ("difficultyMatchRate", "diff-fit", 9, "{:.0%}"),
    ("repeatedAcrossSessionsRate", "repeat", 7, "{:.0%}"),
    ("wrongLanguageCount", "lang!", 6, "{}"),
    ("leakCount", "leak!", 6, "{}"),
)


def _cell(value, fmt: str) -> str:
    return "-" if value is None else fmt.format(value)


async def main(args: argparse.Namespace) -> int:
    async with SessionFactory() as db:
        report = await question_quality_report(
            db,
            since=datetime.fromisoformat(args.since) if args.since else None,
            include_demo=not args.no_demo,
            exclude_email_pattern=args.exclude_email,
            session_id=args.session,
        )
    print(f"Sessions: {report['sessions']} | asked turns: {report['askedTurns']}")
    print("Source mix:", json.dumps(report["sourceMix"], ensure_ascii=False))
    print("Plan difficulty (level/source):", json.dumps(report["planDifficulty"], ensure_ascii=False))
    print()
    print("".join(f"{title:<{width}}" for _, title, width, _ in _COLUMNS))
    for group in report["groups"]:
        print("".join(f"{_cell(group[key], fmt):<{width}}" for key, _, width, fmt in _COLUMNS))
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON written to {args.json}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", help="ISO date; only sessions started on or after it.")
    parser.add_argument("--no-demo", action="store_true", help="Leave out 3-minute demo sessions.")
    parser.add_argument("--exclude-email", help="SQL ILIKE pattern of test accounts to leave out, e.g. 'e2e.%%'.")
    parser.add_argument("--session", help="Report a single session.")
    parser.add_argument("--json", help="Also write the full report to this path.")
    sys.exit(asyncio.run(main(parser.parse_args())))
