"""Admin system health and practice statistics."""

import asyncio
import importlib
from datetime import UTC, datetime, timedelta

health = importlib.import_module("src.modules.admin.system_health")
practice = importlib.import_module("src.modules.admin.practice_stats")


# ---------------------------------------------------------------- health


def test_migration_head_is_the_revision_nothing_builds_on(tmp_path) -> None:
    (tmp_path / "a.py").write_text('revision: str = "0001"\ndown_revision: str | None = None\n')
    (tmp_path / "b.py").write_text('revision: str = "0002"\ndown_revision: str | None = "0001"\n')
    (tmp_path / "c.py").write_text('revision = "0003"\ndown_revision = "0002"\n')
    assert health.migration_head(tmp_path) == "0003"
    assert health.migration_head(tmp_path / "missing") is None


def test_real_migrations_have_one_head() -> None:
    assert health.migration_head() is not None


def test_queue_without_consumer_means_no_worker() -> None:
    ok = {"messages": 0, "consumers": 1}
    assert health.queue_status([ok, ok]) == ("ok", None)
    assert health.queue_status([ok, {"messages": 3, "consumers": 0}]) == ("down", "no_worker")
    assert health.queue_status([ok, {"missing": True, "consumers": None}]) == ("warn", "queue_missing")
    assert health.queue_status([{"messages": 25, "consumers": 2}]) == ("warn", "backlog")


async def test_a_hanging_or_failing_check_does_not_break_the_page(monkeypatch) -> None:
    monkeypatch.setattr(health, "CHECK_TIMEOUT", 0.05)

    async def hang():
        await asyncio.sleep(1)

    async def boom():
        raise RuntimeError("refused")

    async def missing():
        raise ImportError("livekit")

    assert (await health.guarded("x", hang))["detail"] == "timeout"
    failed = await health.guarded("x", boom)
    assert failed["status"] == "down" and "refused" in failed["data"]["error"]
    assert (await health.guarded("x", missing))["status"] == "off"


def test_open_rooms_without_voice_events_flag_the_agent() -> None:
    now = datetime(2026, 10, 26, 8, tzinfo=UTC)
    livekit = health.check("livekit", "ok", None, rooms=2, activeRooms=1)
    recent = health.check("voiceAgent", "ok", None, lastEventAt=(now - timedelta(seconds=30)).isoformat())
    stale = health.check("voiceAgent", "ok", None, lastEventAt=(now - timedelta(hours=2)).isoformat())

    assert health.agent_status(recent, livekit, now)["status"] == "ok"
    assert health.agent_status(stale, livekit, now)["detail"] == "rooms_without_agent"
    idle_livekit = health.check("livekit", "ok", None, rooms=0, activeRooms=0)
    assert health.agent_status(stale, idle_livekit, now)["status"] == "ok"


async def test_overall_is_the_worst_configured_check(monkeypatch) -> None:
    async def _result(status):
        return health.check("k", status)

    monkeypatch.setattr(health, "database_check", lambda db: _result("ok"))
    monkeypatch.setattr(health, "voice_agent_check", lambda db: _result("ok"))
    monkeypatch.setattr(health, "rabbitmq_check", lambda: _result("warn"))
    monkeypatch.setattr(health, "livekit_check", lambda: _result("off"))
    monkeypatch.setattr(health, "langfuse_check", lambda: _result("ok"))

    view = await health.health_view(db=None)
    assert view["overall"] == "warn"
    assert len(view["checks"]) == 5


# ---------------------------------------------------------------- practice

NOW = datetime(2026, 10, 26, 3, tzinfo=UTC)  # Monday 10:00 in Vietnam


def _row(uid, started, *, graded=True, answered=4, score=6.0, status="CLOSED"):
    return {
        "id": f"{uid}-{started.isoformat()}",
        "user_id": uid,
        "status": status,
        "started_at": started,
        "eval_id": "e" if graded else None,
        "overall_score": score if graded else None,
        "answered": answered,
    }


def test_weeks_start_on_monday_in_vietnam_time() -> None:
    # Sunday 18:00 UTC is already Monday 01:00 in Vietnam.
    assert practice.week_of(datetime(2026, 10, 25, 18, tzinfo=UTC)).isoformat() == "2026-10-26"
    assert practice.week_of(datetime(2026, 10, 25, 16, tzinfo=UTC)).isoformat() == "2026-10-19"


def test_weekly_series_counts_valid_sessions_and_goals() -> None:
    last_week = datetime(2026, 10, 21, 3, tzinfo=UTC)
    this_week = datetime(2026, 10, 26, 2, tzinfo=UTC)
    rows = [
        _row("a", last_week),
        _row("a", last_week + timedelta(days=1)),
        _row("b", last_week),
        _row("b", last_week, answered=2),  # under the answer floor
        _row("c", last_week, graded=False, status="ACTIVE"),
        _row("a", this_week, score=8.0),
    ]
    view = practice.summarize_practice(
        rows, {"a": 2, "b": 1}, weeks=3, now=NOW, reports=[{"result": "passed"}]
    )

    assert [w["week"] for w in view["weeks"]] == ["2026-10-12", "2026-10-19", "2026-10-26"]
    previous, current = view["weeks"][1], view["weeks"][2]
    assert previous == {
        "week": "2026-10-19",
        "sessions": 5,
        "graded": 4,
        "valid": 3,
        "activeUsers": 3,
        "practisingUsers": 2,
        "metGoal": 2,  # a: 2 of 2, b: 1 of 1
        "avgScore": 6.0,
    }
    assert current["valid"] == 1 and current["metGoal"] == 0 and current["avgScore"] == 8.0
    assert view["weeks"][0]["sessions"] == 0

    totals = view["totals"]
    assert totals["short"] == 1 and totals["users"] == 3 and totals["practisingUsers"] == 2
    assert totals["returningUsers"] == 1  # only a practised in two weeks
    assert view["goalDistribution"] == {1: 1, 2: 2}  # c has no goal set: default 2
    assert view["realReports"]["passed"] == 1


def test_sessions_outside_the_window_are_ignored() -> None:
    old = _row("a", NOW - timedelta(weeks=10))
    view = practice.summarize_practice([old], {}, weeks=4, now=NOW)
    assert view["totals"]["sessions"] == 0 and view["totals"]["medianSessionsPerUser"] is None


def test_langfuse_llm_connections_are_informational() -> None:
    from src.modules.admin.system_health import langfuse_status

    assert langfuse_status(None, None) == ("down", "unreachable")
    # Organization connections are invisible to the project API: no false alarm.
    assert langfuse_status("https://lf/project/p", []) == ("ok", None)
    assert langfuse_status("https://lf/project/p", ["openai"]) == ("ok", None)
