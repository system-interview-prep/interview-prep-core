"""Admin session list: stats cover every matching session and rows carry real data."""

import importlib
from datetime import UTC, datetime
from types import SimpleNamespace

admin_router = importlib.import_module("src.modules.admin.router")


class _Db:
    def __init__(self, rows: list[dict], summary: dict) -> None:
        self.rows = rows
        self.summary = summary
        self.statements: list[tuple[str, dict]] = []

    async def execute(self, statement, params):
        sql = str(statement)
        self.statements.append((sql, params))
        if "COUNT(*) AS total" in sql:
            return SimpleNamespace(mappings=lambda: SimpleNamespace(one=lambda: self.summary))
        return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: self.rows))


def _row(**overrides) -> dict:
    row = {
        "id": "ses-1",
        "status": "CLOSED",
        "started_at": datetime(2026, 10, 9, 9, 5, tzinfo=UTC),
        "locale": "vi-VN",
        "language": None,
        "effective_mode": "text",
        "job_title": "Backend Developer",
        "candidate_name": "Nguyễn An",
        "candidate_email": "an@example.com",
        "ai_score": 7.4,
        "user_id": "u-1",
        "end_reason": "COMPLETED",
    }
    row.update(overrides)
    return row


async def test_stats_come_from_the_aggregate_not_the_page() -> None:
    db = _Db([_row()], {"total": 137, "active": 12, "completed": 125, "avg_score": 6.83})

    body = await admin_router.list_admin_sessions(
        mode="all", search=None, limit=10, offset=20, _={"sub": "admin"}, db=db
    )

    assert body["total"] == 137
    assert body["stats"] == {"active": 12, "completed": 125, "ungraded": 0, "avgScore": 68}
    page_sql, page_params = db.statements[0]
    assert page_params["limit"] == 10 and page_params["offset"] == 20
    # The latest evaluation only, so re-graded sessions don't duplicate rows.
    assert "LEFT JOIN LATERAL" in page_sql


async def test_rows_use_real_job_title_and_mode() -> None:
    db = _Db(
        [_row(effective_mode="voice", job_title=None, ai_score=None, status="OPEN")],
        {"total": 1, "active": 1, "completed": 0, "avg_score": None},
    )

    body = await admin_router.list_admin_sessions(
        mode="voice", search="an", limit=50, offset=0, _={"sub": "admin"}, db=db
    )

    item = body["sessions"][0]
    assert item["position"] is None  # no invented role when the JD is gone
    assert item["type"] == "voice" and item["status"] == "scheduled" and item["aiScore"] is None
    assert body["stats"]["avgScore"] is None
    page_sql, page_params = db.statements[0]
    assert "COALESCE(s.mode, s.type, 'text')" in page_sql and "= 'voice'" in page_sql
    assert page_params["kw"] == "%an%"


async def test_closed_sessions_without_a_report_need_action() -> None:
    db = _Db(
        [_row(ai_score=None, end_reason="CANDIDATE_LEFT"), _row(id="ses-2")],
        {"total": 2, "active": 0, "completed": 2, "ungraded": 1, "avg_score": 7.4},
    )

    body = await admin_router.list_admin_sessions(
        mode="all", search=None, report="missing", limit=10, offset=0, _={"sub": "admin"}, db=db
    )

    first, second = body["sessions"]
    assert first["status"] == "action_needed" and first["endReason"] == "CANDIDATE_LEFT"
    assert second["status"] == "completed" and second["userId"] == "u-1"
    assert body["stats"]["ungraded"] == 1
    page_sql, _ = db.statements[0]
    assert "ev.overall_score IS NULL" in page_sql


def test_access_log_kinds_split_reads_from_changes() -> None:
    assert admin_router._access_log_filter("all") == ("", {})
    reads, params = admin_router._access_log_filter("reads")
    assert reads == "WHERE l.action = ANY(:actions)" and "VIEW_USER_ACTIVITY" in params["actions"]
    changes, _ = admin_router._access_log_filter("changes")
    assert changes.startswith("WHERE NOT")
