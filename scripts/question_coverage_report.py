"""Print Question Bank coverage (sessions and ACTIVE jobs) as tables for the thesis report.

Usage:
    python scripts/question_coverage_report.py            # report only
    python scripts/question_coverage_report.py --prepare  # pre-generate first (LLM calls), then report
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.infrastructure.database import SessionFactory  # noqa: E402
from src.modules.interviews.planning.question_coverage import (  # noqa: E402
    prepare_job_question_coverage,
    question_coverage_report,
)


async def main(prepare: bool) -> int:
    async with SessionFactory() as db:
        if prepare:
            print("prepare:", json.dumps(await prepare_job_question_coverage(db), ensure_ascii=False))
        report = await question_coverage_report(db)

    print(f"\nSessions (locked plans) — min {report['minQuestionsPerSkill']} questions/skill")
    print(f"{'group':<9}{'sessions':>9}{'reviewed only':>15}{'with generated':>16}{'with uncovered':>16}")
    for group, stats in sorted(report["sessions"].items()):
        print(
            f"{group:<9}{stats['sessions']:>9}{stats['fullyReviewedPct']:>14}%"
            f"{stats['withGeneratedPct']:>15}%{stats['withUncoveredPct']:>15}%"
        )
        print(f"{'':<9}question sources: {stats['questionSources']}")

    skills = report["mustHaveSkills"]
    print("\nMust-have skills of ACTIVE jobs")
    print(
        f"total {skills['total']} | reviewed only {skills['reviewedOnlyPct']}% | "
        f"needs generated {skills['withGeneratedPct']}% | short {skills['shortPct']}%"
    )
    for job in report["jobs"]:
        flag = f"short: {', '.join(job['shortSkills'])}" if job["shortSkills"] else "ok"
        print(f"  {str(job['title'])[:60]:<62}{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--prepare" in sys.argv)))
