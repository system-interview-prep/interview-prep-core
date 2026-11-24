"""Admin user detail and the read-only settings view."""

import importlib
from datetime import UTC, date, datetime
from types import SimpleNamespace

activity = importlib.import_module("src.modules.admin.user_activity")
system_config = importlib.import_module("src.modules.admin.system_config")


class _Db:
    """Answers each query by a marker in its SQL; a missing marker raises like a missing table."""

    def __init__(self, answers: dict[str, list[dict]]) -> None:
        self.answers = answers
        self.rollbacks = 0

    async def execute(self, statement, params):
        sql = str(statement)
        for marker, rows in self.answers.items():
            if marker in sql:
                return SimpleNamespace(mappings=lambda rows=rows: SimpleNamespace(all=lambda: rows))
        raise RuntimeError(f"relation does not exist: {sql[:40]}")

    async def rollback(self) -> None:
        self.rollbacks += 1


USER = {
    "id": "u1",
    "email": "an@example.com",
    "name": "Nguyễn An",
    "provider": "local",
    "is_active": True,
    "created_at": datetime(2026, 10, 1, tzinfo=UTC),
    "practice_weekly_goal": 3,
    "roles": ["CANDIDATE"],
}


def _session(sid, status, score, mode="voice") -> dict:
    return {
        "id": sid,
        "status": status,
        "mode": mode,
        "started_at": datetime(2026, 10, 20, 9, tzinfo=UTC),
        "ended_at": None,
        "end_reason": None,
        "duration_minutes": 15,
        "job_title": "Backend Developer",
        "score": score,
    }


async def test_unknown_user_is_none() -> None:
    assert await activity.user_activity(_Db({"FROM users u": []}), "nope") is None


async def test_view_collects_every_section_and_tolerates_missing_tables() -> None:
    db = _Db(
        {
            "FROM users u": [USER],
            "FROM user_cvs": [
                {
                    "id": "cv1",
                    "filename": "an.pdf",
                    "status": "failed",
                    "error": "x" * 500,
                    "created_at": datetime(2026, 10, 2, tzinfo=UTC),
                    "updated_at": None,
                }
            ],
            "COUNT(*) AS n": [
                {"n": 42, "closed": 40, "graded": 30, "ungraded": 10, "avg_score": 6.123, "best_score": 9.1}
            ],
            "FROM interview_sessions s": [
                _session("s1", "CLOSED", 7.5),
                _session("s2", "CLOSED", None, mode="call"),
                _session("s3", "ACTIVE", None, mode=None),
            ],
            "FROM matching_pipeline_events": [
                {
                    "run_id": "r1",
                    "started_at": datetime(2026, 10, 3, tzinfo=UTC),
                    "job_id": "j1",
                    "has_error": False,
                    "done": True,
                }
            ],
            "WHERE id = ANY(:ids)": [{"id": "j1", "title": "Data Engineer"}],
            # real_interview_reports is missing: empty list, not an error.
        }
    )

    view = await activity.user_activity(db, "u1")

    assert view["user"]["roles"] == ["CANDIDATE"] and view["user"]["weeklyGoal"] == 3
    assert view["cvs"][0]["status"] == "FAILED" and len(view["cvs"][0]["error"]) == activity.ERROR_CHARS
    assert [s["mode"] for s in view["sessions"]] == ["voice", "video", "chat"]
    assert view["summary"] == {
        "sessions": 42,
        "closed": 40,
        "graded": 30,
        "ungraded": 10,
        "open": 2,
        "avgScore": 6.12,
        "bestScore": 9.1,
        "listed": 3,
        "cvs": 1,
        "cvsFailed": 1,
        "matchingRuns": 1,
        "realReports": 0,
    }
    assert view["matchingRuns"][0] == {
        "runId": "r1",
        "startedAt": "2026-10-03T00:00:00+00:00",
        "jobTitle": "Data Engineer",
        "status": "done",
    }
    assert view["realReports"] == [] and db.rollbacks == 1


async def test_real_reports_are_listed() -> None:
    db = _Db(
        {
            "FROM users u": [USER],
            "FROM real_interview_reports": [
                {
                    "id": "rr1",
                    "company": "ACME",
                    "interview_date": date(2026, 10, 10),
                    "stage": "TECH",
                    "result": "PASS",
                }
            ],
        }
    )
    view = await activity.user_activity(db, "u1")
    assert view["realReports"] == [
        {"id": "rr1", "company": "ACME", "date": "2026-10-10", "stage": "TECH", "result": "PASS"}
    ]


def test_settings_view_never_returns_secrets(monkeypatch) -> None:
    from src.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "sk-secret-value")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk-lf-secret")
    monkeypatch.setenv("INTERVIEW_MIN_MATCH_PERCENT", "55")

    view = system_config.config_view()

    text = repr(view)
    assert "sk-secret-value" not in text and "sk-lf-secret" not in text
    groups = {g["key"]: {i["key"]: i for i in g["items"]} for g in view["groups"]}
    assert set(groups) == {"entry", "runtime", "speech", "documents", "monitoring", "system"}
    assert groups["system"]["openaiKey"]["value"] is True
    assert groups["entry"]["minMatchPercent"] == {
        "key": "minMatchPercent",
        "value": 55,
        "env": "INTERVIEW_MIN_MATCH_PERCENT",
        "default": 0,
        "changed": True,
    }
    # Fixed in code: no variable, never "changed".
    assert groups["speech"]["probeMaxWords"]["env"] is None
    assert groups["speech"]["probeMaxWords"]["changed"] is False


def test_summary_falls_back_to_the_listed_sessions() -> None:
    items = [
        {"status": "CLOSED", "score": 7.5},
        {"status": "CLOSED", "score": None},
        {"status": "ACTIVE", "score": None},
    ]
    assert activity.summarize(items) == {
        "sessions": 3,
        "closed": 2,
        "graded": 1,
        "ungraded": 1,
        "open": 1,
        "avgScore": 7.5,
        "bestScore": 7.5,
    }
