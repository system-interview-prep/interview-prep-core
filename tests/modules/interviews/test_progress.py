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
    kalman_estimates,
    reliable_trend,
    skill_levels,
    trend,
    baseline_label,
    round_ladder,
    weekly_streak,
)
from src.modules.interviews.application.progress_calibration import (
    Calibration,
    SkillSessionPoint,
    between_person_sd,
    grading_sd,
    tier_offsets,
    typical_error,
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
    expected = {
        "technicalTurns": 3, "answeredTurns": 2, "firstTimeSufficient": 1,
        "firstTimeSufficientRate": 0.5, "probesNeeded": 1, "skipped": 1,
    }
    assert {key: behavior[key] for key in expected} == expected


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
    # Started this week, but no answers: it does not count toward the streak.
    assert progress["streak"]["startedThisWeek"] == 1
    assert progress["streak"]["thisWeekCount"] == 0


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


# --- Bằng chứng tiến bộ (nghiên cứu 20/10) ------------------------------------


def _checklist(*statuses: str) -> list[dict]:
    return [{"key": f"p{i}", "importance": "CORE", "status": status} for i, status in enumerate(statuses)]


def test_reliable_trend_needs_a_change_beyond_the_noise() -> None:
    default = Calibration()  # typical error 1.2, worthwhile change 0.3
    # The old 0.5 band called this progress; with session noise it is not.
    assert reliable_trend([5.0, 5.2, 5.0, 5.8, 5.9, 5.7], default)["status"] == "flat"
    clear = reliable_trend([4.0, 4.5, 4.2, 6.0, 6.4, 6.6], default)
    assert clear["status"] == "up" and clear["probability"] >= 0.8
    assert reliable_trend([7.0, 7.2, 7.1, 5.0, 4.8, 5.2], default)["status"] == "down"
    assert reliable_trend([5.0, 6.0], default)["status"] == "insufficient"
    # A quieter system (measured) can call a smaller change.
    quiet = Calibration(typical_error=0.4, between_sd=1.0)
    assert reliable_trend([5.0, 5.2, 5.0, 5.8, 5.9, 5.7], quiet)["status"] == "up"


def test_kalman_estimate_smooths_and_narrows() -> None:
    estimates = kalman_estimates([5.0, 8.0, 5.0, 8.0], Calibration())
    means = [mean for mean, _ in estimates]
    sds = [sd for _, sd in estimates]
    assert means[0] == 5.0
    # A jump of 3 points moves the estimate only part of the way.
    assert 5.0 < means[1] < 8.0
    assert sds[0] > sds[-1]


def test_skill_level_changes_only_past_the_margin() -> None:
    level, changes = skill_levels([5.6, 7.1, 7.4, 6.9, 6.75])
    # 7.1 is within the 0.3 margin above 7.0: still "Đạt yêu cầu" (2); 7.4 moves up;
    # 6.9 and 6.75 stay within the margin below 7.0.
    assert changes == [(2, 2, 3)]
    assert level == 3
    level, changes = skill_levels([7.5, 6.5])
    assert changes == [(1, 3, 2)] and level == 2


def test_session_behavior_counts_checklist_evidence() -> None:
    sessions = [_session(1)]
    turns = [
        TurnRow("s1", "DEEP_DIVE", "skill-a", "A", None, 9.0, "ANSWERED",
                rubric_points=_checklist("COVERED", "COVERED")),
        TurnRow("s1", "DEEP_DIVE", "skill-b", "B", None, 5.0, "ANSWERED", needed_probe=True,
                rubric_points=_checklist("COVERED", "COVERED", "COVERED", "PARTIAL")),
        TurnRow("s1", "CHALLENGE", "skill-c", "C", None, 3.0, "ANSWERED", needed_probe=True,
                rubric_points=_checklist("INCORRECT", "MISSING")),
        TurnRow("s1", "BEHAVIORAL", None, None, None, 7.0, "ANSWERED",
                star={"is_star_complete": True, "result": "giảm thời gian phản hồi 40%"}),
        TurnRow("s1", "VALIDATE", "skill-d", "D", None, 6.0, "ANSWERED",
                star={"is_star_complete": False, "result": "mọi người hài lòng"},
                rubric_points=_checklist("COVERED")),
    ]
    behavior = build_progress(sessions, turns, now=NOW)["sessions"][0]["behavior"]
    assert behavior["checklistTurns"] == 4
    assert behavior["coreTotal"] == 9 and behavior["coreCovered"] == 6.5
    assert behavior["fullCoreTurns"] == 2
    assert behavior["incorrectTurns"] == 1
    assert behavior["probesNeeded"] == 2 and behavior["recoveredAfterProbe"] == 1
    assert behavior["starTurns"] == 2 and behavior["starComplete"] == 1 and behavior["measurableResult"] == 1


def test_skill_view_has_estimate_level_and_compares_within_tier() -> None:
    sessions = [_session(1), _session(2), _session(3), _session(4, tier="challenge")]
    turns = [
        _turn("s1", "skill-redis", 4.0), _turn("s2", "skill-redis", 5.5),
        _turn("s3", "skill-redis", 7.0), _turn("s4", "skill-redis", 5.0),
    ]
    redis = build_progress(sessions, turns, now=NOW)["skills"][0]
    # Only the challenge point is comparable with the latest (challenge) one.
    assert redis["comparablePoints"] == 1
    assert redis["points"][0]["estimate"] is None and redis["points"][-1]["estimate"] == 5.0
    # With measured tier offsets every point is moved onto one scale.
    calibrated = Calibration(tier_offsets={"standard": 0.0, "challenge": 1.5}, measured_at=NOW)
    redis = build_progress(sessions, turns, now=NOW, calibration=calibrated)["skills"][0]
    assert redis["comparablePoints"] == 4
    assert redis["trend"]["status"] == "up"
    public = build_progress(sessions, turns, now=NOW, calibration=calibrated)["calibration"]
    assert public["tierAdjusted"] is True


def test_trend_is_read_within_one_grader_version() -> None:
    sessions = [_session(i) for i in range(1, 5)]
    for s, version in zip(sessions, ["m1|v1", "m1|v1", "m2|v1", "m2|v1"], strict=True):
        s.grader_version = version
    scores = [3.0, 3.5, 7.0, 7.5]
    turns = [_turn(f"s{i}", "skill-a", score) for i, score in zip(range(1, 5), scores, strict=True)]
    skill = build_progress(sessions, turns, now=NOW)["skills"][0]
    # The jump came with a grader change: not shown as progress.
    assert skill["comparablePoints"] == 2
    assert skill["trend"]["status"] == "insufficient"


def test_session_recap_lists_firsts_and_level_changes() -> None:
    sessions = [_session(1), _session(2), _session(3)]
    turns = [
        TurnRow("s1", "DEEP_DIVE", "skill-a", "A", None, 4.0, "ANSWERED",
                rubric_points=_checklist("COVERED", "MISSING")),
        TurnRow("s2", "DEEP_DIVE", "skill-a", "A", None, 5.6, "ANSWERED",
                rubric_points=_checklist("COVERED", "PARTIAL")),
        TurnRow("s3", "DEEP_DIVE", "skill-a", "A", None, 9.5, "ANSWERED",
                rubric_points=_checklist("COVERED", "COVERED")),
        TurnRow("s3", "BEHAVIORAL", None, None, None, 8.0, "ANSWERED",
                star={"is_star_complete": True, "result": "tăng 20%"}),
    ]
    progress = build_progress(sessions, turns, now=NOW)
    comparison = compare_with_previous(progress, "s3")
    assert set(comparison["firsts"]) >= {"full_core", "star_complete", "measurable_result"}
    assert comparison["levelChanges"] and comparison["levelChanges"][0]["skill"] == "A"
    assert comparison["behavior"]["coreCoverage"] == {"before": 0.75, "now": 1.0}
    assert any(m["kind"] == "skill_level_up" for m in progress["milestones"])


def test_calibration_measurements() -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    points = []
    for user in range(6):
        for k in range(4):
            noise = 0.8 if k % 2 else -0.8
            moment = start + timedelta(days=3 * k)
            points.append(SkillSessionPoint(f"u{user}", "redis", "standard", moment, 5 + user * 0.5 + noise))
    te, pairs = typical_error(points)
    import statistics

    diffs = [1.6, -1.6, 1.6] * 6
    assert pairs == 18 and te is not None and abs(te - statistics.pstdev(diffs) / 2**0.5) < 0.01
    bsd, people = between_person_sd(points)
    assert people == 6 and bsd is not None and bsd > 0.8
    challenge = [SkillSessionPoint(f"c{i}", "redis", "challenge", start, 4.0) for i in range(25)]
    offsets = tier_offsets(points + challenge)
    assert offsets["standard"] == 0.0 and offsets["challenge"] > 1.5
    assert tier_offsets(points) == {}
    gsd, n = grading_sd([[6.0, 7.0, 6.5], [4.0, 4.0]])
    assert n == 2 and gsd is not None and 0 < gsd < 1


# --- Tên kỹ năng ứng viên hiểu được (20/10 chiều) -------------------------------


def test_skills_use_candidate_names_areas_and_hide_the_greeting() -> None:
    from src.modules.interviews.application.skill_display import GENERAL_NAME

    sessions = [_session(1)]
    turns = [
        TurnRow("s1", "WARM_UP", "warmup", "Giới thiệu & Khởi động", None, 8.0, "ANSWERED"),
        TurnRow("s1", "VALIDATE", "cv-validation", "Xác thực CV & Dự án", None, 6.0, "ANSWERED"),
        TurnRow("s1", "BEHAVIORAL", "soft-skill.star", "Kỹ năng mềm & Tình huống (STAR)", None, 7.0,
                "ANSWERED"),
        TurnRow("s1", "DEEP_DIVE", "technology.software-engineering.backend", "Backend Engineering", None,
                5.0, "ANSWERED"),
        TurnRow("s1", "DEEP_DIVE", "skill-redis", "Redis", None, 6.5, "ANSWERED"),
        TurnRow("s1", "DEEP_DIVE", "skill-container-orchestrator", "Kubernetes or Docker Swarm", None, 4.0,
                "ANSWERED"),
    ]
    skills = {s["label"]: s for s in build_progress(sessions, turns, now=NOW)["skills"]}
    assert "Giới thiệu & Khởi động" not in skills
    assert skills["Kể về dự án trong CV"]["area"] == "project"
    assert skills["Kể kinh nghiệm làm việc và xử lý tình huống"]["area"] == "teamwork"
    assert skills[GENERAL_NAME]["area"] == "general"
    assert skills["Redis"]["area"] == "data"
    orchestrator = skills["Điều phối container (Kubernetes, Docker Swarm)"]
    assert orchestrator["area"] == "infra" and orchestrator["taxonomyLabel"] == "Kubernetes or Docker Swarm"


def test_each_skill_lists_its_recent_questions_and_missed_points() -> None:
    sessions = [_session(1), _session(2)]
    points = [
        {"importance": "CORE", "status": "COVERED", "description": "Nêu TTL cho key"},
        {"importance": "CORE", "status": "MISSING", "description": "Xử lý cache stampede"},
        {"importance": "CORE", "status": "INCORRECT", "description": "Phân biệt write-through và write-back"},
        {"importance": "BONUS", "status": "MISSING", "description": "Đo hit rate"},
    ]
    turns = [
        TurnRow("s1", "DEEP_DIVE", "skill-redis", "Redis", None, 5.0, "ANSWERED",
                question="Bạn dùng Redis làm gì?"),
        TurnRow("s2", "DEEP_DIVE", "skill-redis", "Redis", None, 6.0, "ANSWERED", rubric_points=points,
                question="Khi cache hết hạn cùng lúc thì sao?"),
    ]
    redis = build_progress(sessions, turns, now=NOW)["skills"][0]
    latest = redis["recentQuestions"][0]
    assert latest["question"] == "Khi cache hết hạn cùng lúc thì sao?" and latest["sessionId"] == "s2"
    missed = [m["point"] for m in latest["missing"]]
    assert missed == ["Xử lý cache stampede", "Phân biệt write-through và write-back"]
    assert redis["recentQuestions"][1]["missing"] == []
    assert latest["checked"] is True
    # No checklist was recorded: the page must not claim every point was covered.
    assert redis["recentQuestions"][1]["checked"] is False


def test_a_story_question_lists_the_star_parts_it_left_out() -> None:
    turns = [
        TurnRow("s1", "BEHAVIORAL", "soft-skill.star", "STAR", None, 6.0, "ANSWERED",
                star={"situation": "dự án gấp", "task": "", "action": "chia việc", "result": ""},
                question="Kể về lần bạn bất đồng với đồng nghiệp."),
    ]
    story = build_progress([_session(1)], turns, now=NOW)["skills"][0]
    asked = story["recentQuestions"][0]
    assert asked["checked"] is True
    assert [m["point"] for m in asked["missing"]] == ["Nhiệm vụ của bạn", "Kết quả (tốt nhất có số liệu)"]


def test_gap_notebook_follows_a_point_across_sessions() -> None:
    def pts(stampede: str, ttl: str) -> list[dict[str, str]]:
        return [
            {"importance": "CORE", "status": stampede, "description": "Xử lý cache stampede"},
            {"importance": "CORE", "status": ttl, "description": "Nêu TTL cho key"},
        ]

    sessions = [_session(1), _session(2), _session(3)]
    turns = [
        TurnRow("s1", "DEEP_DIVE", "skill-redis", "Redis", None, 4.0, "ANSWERED", rubric_points=pts("MISSING", "MISSING")),
        TurnRow("s2", "DEEP_DIVE", "skill-redis", "Redis", None, 5.0, "ANSWERED", rubric_points=pts("PARTIAL", "COVERED")),
        TurnRow("s3", "DEEP_DIVE", "skill-redis", "Redis", None, 6.0, "ANSWERED", rubric_points=pts("MISSING", "COVERED"),
                question="Cache hết hạn cùng lúc thì sao?"),
        # XML is no longer asked on its own: kept out of the notebook.
        TurnRow("s3", "DEEP_DIVE", "skill-xml", "XML", None, 1.0, "ANSWERED",
                rubric_points=[{"importance": "CORE", "status": "MISSING", "description": "XSD"}]),
    ]
    progress = build_progress(sessions, turns, now=NOW)
    gaps = progress["gaps"]
    assert [g["point"] for g in gaps["open"]] == ["Xử lý cache stampede"]
    stampede = gaps["open"][0]
    assert (stampede["timesMissed"], stampede["timesAsked"]) == (3, 3)
    assert stampede["lastSeen"]["sessionId"] == "s3" and stampede["area"] == "data"
    assert [g["point"] for g in gaps["fixed"]] == ["Nêu TTL cho key"]
    assert next(s for s in progress["skills"] if s["conceptId"] == "skill-xml")["minor"] is True


def test_readiness_is_shown_by_interview_round() -> None:
    sessions = [_session(1), _session(2), _session(3)]
    turns = []
    for sid, sql, redis in (("s1", 6.0, 5.0), ("s2", 7.5, 5.5), ("s3", 8.0, 6.0)):
        turns.append(TurnRow(sid, "DEEP_DIVE", "skill-sql", "SQL", None, sql, "ANSWERED"))
        turns.append(TurnRow(sid, "DEEP_DIVE", "skill-redis", "Redis", None, redis, "ANSWERED"))
    turns.append(TurnRow("s3", "DEEP_DIVE", "skill-docker", "Docker", None, 4.0, "ANSWERED",
                         rubric_points=[{"importance": "CORE", "status": "MISSING", "description": "Multi-stage build"}]))
    turns.append(TurnRow("s3", "DEEP_DIVE", "skill-xml", "XML", None, 0.3, "ANSWERED"))
    progress = build_progress(sessions, turns, now=NOW)
    rounds = {r["area"]: r for r in progress["readiness"][0]["rounds"]}
    # XML is not a round of its own any more; project and teamwork are always listed.
    assert set(rounds) == {"project", "data", "infra", "teamwork"}
    assert rounds["data"]["sessions"] == 3 and rounds["data"]["status"] in {"almost", "ready"}
    assert rounds["infra"]["status"] == "thin"
    assert rounds["infra"]["topGap"]["point"] == "Multi-stage build"
    assert rounds["project"]["status"] == "unseen"


def _answers(session: str, n: int, concept: str = "skill-sql") -> list[TurnRow]:
    return [TurnRow(session, "DEEP_DIVE", concept, "SQL", None, 6.0, "ANSWERED", order=i) for i in range(n)]


def test_only_sessions_with_real_answers_keep_the_streak() -> None:
    """Research 24/10: an effort floor (3 answered main questions), not a score floor."""
    monday = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    sessions = [
        SessionRow("ok", "job-be", "Backend", monday, 3.0, "challenge"),
        SessionRow("thin", "job-be", "Backend", monday + timedelta(hours=2), 8.0, "standard"),
    ]
    turns = _answers("ok", 3) + _answers("thin", 2)
    turns.append(TurnRow("thin", "WARM_UP", "warmup", "Chào", None, 9.0, "ANSWERED"))
    progress = build_progress(sessions, turns, now=monday + timedelta(hours=5), weekly_goal=2)
    assert [s["valid"] for s in progress["sessions"]] == [True, False]
    assert progress["streak"]["thisWeekCount"] == 1


def test_a_missed_week_can_be_earned_back_the_week_after() -> None:
    # Goal 2. Sep 21 met; Sep 28 missed (September's freeze); Oct 5 met;
    # Oct 12 missed (October's freeze); Oct 19 (last week) missed again.
    days = [date(2026, 9, 21)] * 2 + [date(2026, 10, 5)] * 2
    today = date(2026, 10, 28)  # week of Oct 26
    pending = weekly_streak(days + [date(2026, 10, 26)], today=today, goal=2)
    assert pending["earnBack"] == {"weekStart": "2026-10-19", "remaining": 2}
    assert pending["weeks"] == 0
    # Goal + 1 = 3 sessions this week repair last week; the chain holds.
    repaired = weekly_streak(days + [date(2026, 10, 26)] * 3, today=today, goal=2)
    assert repaired["earnBack"] is None
    by_week = {w["weekStart"]: w for w in repaired["history"]}
    assert by_week["2026-10-19"]["repaired"] is True and by_week["2026-10-12"]["frozen"] is True
    assert repaired["weeks"] == 4  # Sep 21, Oct 5, Oct 19 (repaired), this week


def test_wins_and_progress_weeks_come_from_points_fixed_in_a_new_question() -> None:
    def point(status: str) -> list[dict[str, str]]:
        return [{"importance": "CORE", "status": status, "description": "Xử lý cache stampede"}]

    sessions = [_session(1), _session(2), _session(3)]
    turns = [
        TurnRow("s1", "DEEP_DIVE", "skill-redis", "Redis", None, 4.0, "ANSWERED",
                rubric_points=point("MISSING"), question="Cache hết hạn cùng lúc thì sao?"),
        # Same question again, now covered: may be memory, not a win.
        TurnRow("s2", "DEEP_DIVE", "skill-redis", "Redis", None, 6.0, "ANSWERED",
                rubric_points=point("COVERED"), question="Cache hết hạn cùng lúc thì sao?"),
        TurnRow("s2", "DEEP_DIVE", "skill-redis", "Redis", None, 4.0, "ANSWERED", rubric_points=[
            {"importance": "CORE", "status": "MISSING", "description": "TTL ngẫu nhiên"}], question="Thiết kế cache?"),
        TurnRow("s3", "DEEP_DIVE", "skill-redis", "Redis", None, 7.0, "ANSWERED", rubric_points=[
            {"importance": "CORE", "status": "COVERED", "description": "TTL ngẫu nhiên"}], question="Hot key?"),
    ]
    progress = build_progress(sessions, turns, now=NOW)
    fixed_wins = [w for w in progress["wins"] if w["kind"] == "fixed"]
    assert [w["point"] for w in fixed_wins] == ["TTL ngẫu nhiên"]
    assert any(w["progress"] for w in progress["streak"]["history"])
    assert progress["activity"]["days"][-1]["fixed"] >= 1


def test_a_missed_point_is_due_for_practice_after_a_week() -> None:
    turns = [TurnRow("s1", "DEEP_DIVE", "skill-redis", "Redis", None, 4.0, "ANSWERED", rubric_points=[
        {"importance": "CORE", "status": "MISSING", "description": "TTL"}])]
    asked = _session(1, days_ago=8)
    gap = build_progress([asked], turns, now=NOW)["gaps"]["open"][0]
    assert gap["due"] is True
    fresh = build_progress([_session(1, days_ago=2)], turns, now=NOW)["gaps"]["open"][0]
    assert fresh["due"] is False


def test_before_and_now_when_a_skill_is_asked_again() -> None:
    def pts(*statuses: str) -> list[dict[str, str]]:
        names = ["Index ghép", "EXPLAIN ANALYZE"]
        return [{"importance": "CORE", "status": st, "description": n} for st, n in zip(statuses, names, strict=True)]

    sessions = [_session(1), _session(2), _session(3)]
    turns = [
        TurnRow("s1", "DEEP_DIVE", "skill-postgresql", "PostgreSQL", None, 4.0, "ANSWERED",
                rubric_points=pts("MISSING", "MISSING"), question="Truy vấn chậm?"),
        TurnRow("s2", "DEEP_DIVE", "skill-postgresql", "PostgreSQL", None, 6.0, "ANSWERED",
                rubric_points=pts("COVERED", "MISSING"), question="Truy vấn chậm?"),
        TurnRow("s3", "DEEP_DIVE", "skill-postgresql", "PostgreSQL", None, 7.0, "ANSWERED",
                rubric_points=pts("COVERED", "COVERED"), question="Thiết kế index cho bảng log?"),
    ]
    progress = build_progress(sessions, turns, now=NOW)
    same = progress["retries"]["s2"][0]
    assert same["sameQuestion"] is True and same["fixedPoints"] == ["Index ghép"]
    other = compare_with_previous(progress, "s3")["retries"][0]
    assert other["sameQuestion"] is False and other["fixedPoints"] == []
    assert (other["now"]["covered"], other["now"]["total"]) == (2, 2)
    assert [m["point"] for m in other["before"]["missing"]] == ["EXPLAIN ANALYZE"]


def test_each_round_shows_where_it_started() -> None:
    sessions = [_session(i) for i in range(1, 5)]
    turns = []
    for i, score in zip(range(1, 5), (4.0, 5.0, 6.5, 7.5), strict=True):
        turns.append(TurnRow(f"s{i}", "DEEP_DIVE", "skill-redis", "Redis", None, score, "ANSWERED"))
    rounds = {r["area"]: r for r in build_progress(sessions, turns, now=NOW)["readiness"][0]["rounds"]}
    assert rounds["data"]["first"] == 4.0
    assert rounds["data"]["trend"]["status"] in {"up", "flat"}


def _series(*scores: float, tier: str = "standard") -> list[dict]:
    return [
        {"sessionId": f"s{i}", "date": f"2026-09-{i + 10:02d}T08:00:00+00:00", "tier": tier, "score": sc}
        for i, sc in enumerate(scores)
    ]


def test_round_ladder_follows_written_rules() -> None:
    """Research 24/10 (Khan mastery): levels move on whole sessions with explicit rules."""
    assert round_ladder([])["level"] == 0
    assert round_ladder(_series(4.0))["next"] == {"kind": "reach", "score": 5.5, "have": 0, "need": 1}
    basic = round_ladder(_series(6.0, 7.2))
    assert basic["level"] == 1 and basic["next"]["kind"] == "run" and basic["next"]["have"] == 1
    solid = round_ladder(_series(6.0, 7.2, 7.5))
    assert solid["level"] == 2 and [e["to"] for e in solid["levelEvents"]] == [1, 2]
    # The top level needs a solid session at the challenge tier.
    top = round_ladder(_series(6.0, 7.2, 7.5) + [
        {"sessionId": "c", "date": "2026-10-01T08:00:00+00:00", "tier": "challenge", "score": 7.1}])
    assert top["level"] == 3
    # One weak session does not move a level; two in a row do.
    one_low = round_ladder(_series(6.0, 7.2, 7.5, 4.0))
    assert one_low["level"] == 2 and one_low["atRisk"] is True
    assert round_ladder(_series(6.0, 7.2, 7.5, 4.0, 4.5))["level"] == 1


def test_a_session_is_compared_with_the_usual_level_only_with_enough_sessions() -> None:
    assert baseline_label(7.0, [6.0, 6.2], 0.6) is None
    assert baseline_label(7.5, [6.0, 6.2, 6.4, 6.1], 0.6) == "above"
    assert baseline_label(6.3, [6.0, 6.2, 6.4, 6.1], 0.6) == "within"
    assert baseline_label(5.0, [6.0, 6.2, 6.4, 6.1], 0.6) == "below"


def test_readiness_timeline_and_week_tell_what_changed() -> None:
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    sessions, turns = [], []
    for i, score in enumerate((6.0, 7.2, 7.6)):
        sid = f"s{i}"
        sessions.append(SessionRow(sid, "job-be", "Backend", now - timedelta(days=14 - 6 * i), score, "standard"))
        for n in range(3):
            turns.append(TurnRow(sid, "DEEP_DIVE", "skill-python", "Python", None, score, "ANSWERED", order=n,
                                 question=f"q{i}", rubric_points=[
                                     {"importance": "CORE", "status": "COVERED", "description": "A"},
                                     {"importance": "CORE", "status": "MISSING" if i < 2 else "COVERED",
                                      "description": "B"}]))
    progress = build_progress(sessions, turns, now=now)
    job = progress["readiness"][0]
    language = next(r for r in job["rounds"] if r["area"] == "language")
    assert language["level"] == 2 and job["readyCount"] == 1
    assert [w["ready"] for w in job["readyHistory"]][-1] == 1
    history = language["skills"][0]["history"]
    assert [(h["covered"], h["total"]) for h in history][-1] == (2, 2)
    latest = progress["timeline"][0]
    assert latest["sessionId"] == "s2"
    assert latest["levelChanges"] == [{"area": "language", "from": 1, "to": 2}]
    assert progress["week"]["status"] == "improving"
    assert progress["week"]["levelUps"][0]["area"] == "language"
    assert compare_with_previous(progress, "s2")["after"]["levelChanges"][0]["to"] == 2
