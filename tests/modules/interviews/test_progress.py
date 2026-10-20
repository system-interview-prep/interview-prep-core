"""Tiến bộ qua các buổi (application/progress.py): so được, chống dao động, chuỗi tuần."""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime, timedelta

import pytest

from src.modules.interviews.application.progress import (
    SessionRow,
    TurnRow,
    build_progress,
    compare_with_previous,
    trend,
    weekly_streak,
)

NOW = datetime(2026, 10, 19, 4, 0, tzinfo=UTC)  # Monday 11:00 in Vietnam


def _session(i: int, *, job="job-be", score=6.0, tier="standard", days_ago=None) -> SessionRow:
    started = NOW - timedelta(days=days_ago if days_ago is not None else (30 - i * 3))
    return SessionRow(f"s{i}", job, "Backend Developer" if job == "job-be" else "Data Engineer", started, score, tier)


def _turn(session: str, concept: str, score: float, *, stage="DEEP_DIVE", outcome="ANSWERED", probe=False, order=2):
    label = concept.replace("skill-", "").title() if concept else None
    return TurnRow(session, stage, concept, label, "intermediate", score, outcome, probe, order)


def test_trend_needs_three_points_and_ignores_small_changes() -> None:
    assert trend([5.0, 7.0]) == {"status": "insufficient", "delta": None}
    assert trend([5.0, 5.2, 5.3])["status"] == "flat"
    assert trend([4.0, 5.0, 6.0, 6.5, 7.0, 7.5]) == {"status": "up", "delta": 2.0}
    assert trend([7.0, 6.0, 5.0])["status"] == "down"


def test_skills_are_grouped_by_concept_not_by_job_label() -> None:
    sessions = [_session(1), _session(2, job="job-data"), _session(3)]
    turns = [
        _turn("s1", "skill-redis", 4.5),
        TurnRow("s2", "DEEP_DIVE", "skill-redis", "Bộ nhớ đệm Redis", "intermediate", 6.0, "ANSWERED"),
        _turn("s3", "skill-redis", 7.2),
        _turn("s3", "skill-redis", 7.0, order=3),
    ]
    progress = build_progress(sessions, turns, now=NOW)
    redis = next(s for s in progress["skills"] if s["conceptId"] == "skill-redis")
    assert [p["score"] for p in redis["points"]] == [4.5, 6.0, 7.1]
    assert redis["trend"]["status"] == "up"


def test_job_trend_is_read_within_one_tier() -> None:
    # Scores drop when the candidate unlocks challenge: that is a step up, not a decline.
    sessions = [
        _session(1, score=6.0), _session(2, score=6.6), _session(3, score=7.2),
        _session(4, score=6.1, tier="challenge"),
    ]
    progress = build_progress(sessions, [], now=NOW)
    job = progress["jobs"][0]
    assert job["currentTier"] == "challenge"
    assert job["tierChanges"] == [{"sessionId": "s4", "date": job["points"][3]["date"], "from": "standard", "to": "challenge"}]
    assert job["trend"]["status"] == "insufficient"  # only one challenge session so far
    kinds = [m["kind"] for m in progress["milestones"]]
    assert "first_challenge" in kinds and "first_session" in kinds


def test_answer_habits_do_not_count_deepening_probes() -> None:
    sessions = [_session(1)]
    turns = [
        _turn("s1", "skill-a", 6, probe=True),
        _turn("s1", "skill-b", 7, probe=False),
        _turn("s1", "skill-c", 0, outcome="SKIPPED"),
        _turn("s1", None, 8, stage="BEHAVIORAL"),
    ]
    behavior = build_progress(sessions, turns, now=NOW)["sessions"][0]["behavior"]
    assert behavior == {
        "technicalTurns": 3, "answeredTurns": 2, "firstTimeSufficient": 1,
        "firstTimeSufficientRate": 0.5, "probesNeeded": 1, "skipped": 1,
    }


def test_weekly_streak_counts_weeks_and_saves_one_missed_week_a_month() -> None:
    monday = date(2026, 10, 19)
    days = [
        monday,  # this week: 1 of 2, still open
        monday - timedelta(days=5), monday - timedelta(days=6),  # last week: met
        # two weeks ago: missed (saved by October's freeze)
        monday - timedelta(days=20), monday - timedelta(days=19),  # three weeks ago: met
    ]
    streak = weekly_streak(days, today=monday, goal=2)
    assert streak["weeks"] == 2
    assert streak["thisWeekCount"] == 1 and streak["thisWeekRemaining"] == 1
    assert streak["freezeAvailableThisMonth"] is False
    assert [w["frozen"] for w in streak["history"]][-3] is True

    # A second missed week in the same month breaks the streak.
    later = date(2026, 10, 26)
    days2 = [date(2026, 10, 20), date(2026, 10, 21), date(2026, 9, 29), date(2026, 9, 30)]
    # Oct 19 met; Oct 12 missed (October's save); Oct 5 missed again: broken.
    assert weekly_streak(days2, today=later, goal=2)["weeks"] == 1


def test_weekly_streak_uses_the_candidates_local_day() -> None:
    # 18:00 UTC Sunday is already Monday 01:00 in Vietnam: it belongs to the new week.
    sunday_evening_utc = datetime(2026, 10, 18, 18, 0, tzinfo=UTC)
    progress = build_progress([], [], now=NOW, tz_offset_minutes=420, weekly_goal=1,
                              all_session_starts=[sunday_evening_utc])
    assert progress["streak"]["thisWeekCount"] == 1


def test_compare_with_previous_same_job() -> None:
    sessions = [_session(1, score=5.5), _session(2, job="job-data", score=8.0), _session(3, score=6.8)]
    turns = [
        _turn("s1", "skill-redis", 4.5), _turn("s1", "skill-sql", 7.0),
        _turn("s3", "skill-redis", 6.5), _turn("s3", "skill-sql", 6.0),
    ]
    comparison = compare_with_previous(build_progress(sessions, turns, now=NOW), "s3")
    assert comparison["previousSessionId"] == "s1" and comparison["sameJob"] is True
    assert comparison["overall"] == {"before": 5.5, "now": 6.8, "delta": 1.3, "comparable": True, "status": "up"}
    assert comparison["improvedSkills"][0]["skill"] == "Redis"
    assert comparison["focusSkills"][0]["skill"] == "Sql"
    assert compare_with_previous(build_progress(sessions, turns, now=NOW), "s1") is None


def test_compare_across_tiers_marks_overall_not_comparable() -> None:
    sessions = [_session(1, score=7.4), _session(2, score=6.2, tier="challenge")]
    comparison = compare_with_previous(build_progress(sessions, [], now=NOW), "s2")
    assert comparison["overall"]["comparable"] is False
    assert comparison["tier"] == {"before": "standard", "now": "challenge"}


@pytest.mark.asyncio
async def test_weekly_goal_is_validated(monkeypatch) -> None:
    from src.modules.interviews.application import progress as progress_module

    with pytest.raises(ValueError):
        await progress_module.set_weekly_goal(None, "u1", 0)
    router_module = importlib.import_module("src.modules.interviews.api.router")
    assert router_module.WeeklyGoalPayload(weeklyGoal=3).weekly_goal == 3
    assert router_module._tz_offset(5000) == 840


def test_many_skills_across_cvs_and_jobs_stay_filterable() -> None:
    sessions = [
        SessionRow("a1", "job-be", "Backend", NOW - timedelta(days=9), 6.0, resume_id="cv-1", resume_name="CV Backend.pdf"),
        SessionRow("a2", "job-data", "Data", NOW - timedelta(days=5), 6.5, resume_id="cv-2", resume_name="CV Data.pdf"),
        SessionRow("a3", "job-be", "Backend", NOW - timedelta(days=1), 7.0, resume_id="cv-1", resume_name="CV Backend.pdf"),
    ]
    turns = [
        _turn("a1", "skill-redis", 5.0), _turn("a1", "skill-sql", 6.0),
        _turn("a2", "skill-sql", 6.5), _turn("a2", "skill-spark", 5.5),
        _turn("a3", "skill-redis", 6.5),
        # A generated question without a concept id is kept, keyed by its label.
        TurnRow("a3", "CHALLENGE", None, "Thiết kế Rate Limiter", "advanced", 7.0, "ANSWERED"),
    ]
    progress = build_progress(sessions, turns, now=NOW)
    keys = {s["conceptId"] for s in progress["skills"]}
    assert keys == {"skill-redis", "skill-sql", "skill-spark", "label:thiết kế rate limiter"}
    sql = next(s for s in progress["skills"] if s["conceptId"] == "skill-sql")
    assert [(p["jobId"], p["resumeId"]) for p in sql["points"]] == [("job-be", "cv-1"), ("job-data", "cv-2")]
    assert progress["resumes"] == [
        {"resumeId": "cv-1", "name": "CV Backend.pdf", "sessions": 2},
        {"resumeId": "cv-2", "name": "CV Data.pdf", "sessions": 1},
    ]
    assert {j["jobId"] for j in progress["jobs"]} == {"job-be", "job-data"}
