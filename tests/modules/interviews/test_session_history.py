"""The session list carries score and progress so history views need no extra calls."""

import importlib
from datetime import UTC, datetime
from types import SimpleNamespace

interview_router = importlib.import_module("src.modules.interviews.api.router")


def _session_row(session_id: str) -> dict:
    return {
        "id": session_id,
        "resume_id": None,
        "job_id": "job-1",
        "job_title": "Backend Engineer",
        "mode": "text",
        "experience_type": "interview_chat",
        "end_reason": "COMPLETED",
        "locale": "vi-VN",
        "duration_minutes": 25,
        "status": "CLOSED",
        "started_at": datetime(2026, 10, 9, 9, tzinfo=UTC),
        "ended_at": datetime(2026, 10, 9, 9, 20, tzinfo=UTC),
        "plan_id": "plan-1",
        "plan_schema_version": "1.0",
        "plan_status": "LOCKED",
    }


class _Db:
    def __init__(self, sessions: list[dict], summaries: list[dict]) -> None:
        self.sessions = sessions
        self.summaries = summaries
        self.summary_params: dict | None = None

    async def execute(self, statement, params):
        sql = str(statement)
        rows = self.sessions
        if "LEFT JOIN LATERAL" in sql:
            self.summary_params = params
            rows = self.summaries
        return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))


async def test_session_list_includes_latest_score_and_answered_turns() -> None:
    db = _Db(
        [_session_row("ses-1"), _session_row("ses-2")],
        [
            {
                "session_id": "ses-1",
                "overall_score": 6.4,
                "evaluated_at": datetime(2026, 10, 9, 9, 25, tzinfo=UTC),
                "competency_scores": '[{"competency": "Java", "score": 5.5, "weight": 1.0}, '
                '{"competency": "Warm-up", "score": 8, "weight": 0}, {"score": 3}]',
                "total_turns": 5,
                "answered_turns": 4,
            }
        ],
    )

    body = await interview_router.list_interview_sessions(user={"sub": "user-a"}, db=db)

    first, second = body["sessions"]
    assert db.summary_params == {"ids": ["ses-1", "ses-2"]}
    assert (first["overallScore"], first["totalTurns"], first["answeredTurns"]) == (6.4, 5, 4)
    assert first["evaluatedAt"].startswith("2026-10-09T09:25")
    # Competency scores come from the latest evaluation; malformed entries are dropped.
    assert first["competencyScores"] == [
        {"competency": "Java", "score": 5.5, "weight": 1.0},
        {"competency": "Warm-up", "score": 8.0, "weight": 0.0},
    ]
    assert second["competencyScores"] == []
    # A session without an evaluation or turns still gets explicit empty values.
    assert (second["overallScore"], second["totalTurns"], second["answeredTurns"]) == (None, 0, 0)


async def test_empty_session_list_skips_the_summary_query() -> None:
    db = _Db([], [])
    body = await interview_router.list_interview_sessions(user={"sub": "user-a"}, db=db)
    assert body == {"sessions": []}
    assert db.summary_params is None
