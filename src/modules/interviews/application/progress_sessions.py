"""Session aggregation, timeline, comparison, and weekly summaries."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, datetime
from typing import Any

from src.modules.interviews.application.progress_calibration import Calibration
from src.modules.interviews.application.progress_skills import (
    _NUMBER,
    _STAR_STAGES,
    _TECHNICAL_STAGES,
    _TIER_RANK,
    DEFAULT_WEEKLY_GOAL,
    FLAT_DELTA,
    GAP_FIXED_LIMIT,
    GAP_OPEN_LIMIT,
    LEVEL_EDGES,
    LEVEL_MARGIN,
    PROCESS_SD,
    OVERVIEW_MIN_SESSIONS,
    ROOM_KEYS,
    ROOMS,
    READY_LEVEL_INDEX,
    RECENT_QUESTIONS,
    RECOVERED_COVERAGE,
    TREND_MIN_POINTS,
    TREND_PROBABILITY,
    TREND_WINDOW,
    VALID_MIN_ANSWERS,
    SessionRow,
    TurnRow,
    _adjusted,
    _checked,
    _comparable,
    _core_coverage,
    _iso,
    _local_day,
    _missing_points,
    _point_states,
    _round,
    _week_start,
    activity_days,
    gap_notebook,
    kalman_estimates,
    ready_history,
    reliable_trend,
    round_readiness,
    skill_key,
    skill_levels,
    trend,
    weekly_streak,
)
from src.modules.interviews.application.skill_display import area_of, display_name, is_tracked
from src.modules.interviews.core.interview_rounds import is_minor


def _session_behavior(turns: list[TurnRow]) -> dict[str, Any]:
    """How the candidate answered: counts the progress page turns into before/now rates."""
    technical = [t for t in turns if t.stage in _TECHNICAL_STAGES]
    answered = [t for t in technical if t.outcome == "ANSWERED"]
    skipped = [t for t in technical if t.outcome == "SKIPPED"]
    first_time = [t for t in answered if not t.needed_probe]
    probed = [t for t in answered if t.needed_probe]
    assessed = [t for t in turns if t.outcome == "ANSWERED" and t.stage not in {"WARM_UP", "CLOSING", ""}]
    checklist = [t for t in assessed if t.rubric_points]
    core_covered, core_total, full_core, incorrect = 0.0, 0, 0, 0
    for turn in checklist:
        covered, total, full, wrong = _core_coverage(turn)
        core_covered += covered
        core_total += total
        full_core += int(full)
        incorrect += int(wrong)
    recovered = 0
    for turn in probed:
        covered, total, _, _ = _core_coverage(turn)
        if total and covered / total >= RECOVERED_COVERAGE:
            recovered += 1
    star_turns = [t for t in assessed if t.stage in _STAR_STAGES and t.star]
    return {
        "technicalTurns": len(technical),
        "answeredTurns": len(answered),
        "firstTimeSufficient": len(first_time),
        "firstTimeSufficientRate": round(len(first_time) / len(answered), 2) if answered else None,
        "probesNeeded": len(probed),
        "recoveredAfterProbe": recovered,
        "skipped": len(skipped),
        "checklistTurns": len(checklist),
        "coreCovered": round(core_covered, 1),
        "coreTotal": core_total,
        "fullCoreTurns": full_core,
        "incorrectTurns": incorrect,
        "starTurns": len(star_turns),
        "starComplete": sum(1 for t in star_turns if t.star.get("is_star_complete")),
        "measurableResult": sum(1 for t in star_turns if _NUMBER.search(str(t.star.get("result") or ""))),
    }


def valid_session(turns: list[TurnRow]) -> bool:
    """A session that counts toward the weekly streak: enough real answers.

    At least ``VALID_MIN_ANSWERS`` main questions answered (the greeting and
    the closing Q&A do not count). Abandoned or empty sessions do not keep a
    streak alive (GitHub's streak counter produced one-contribution days to
    keep a streak; research 24/10).
    """
    answered = [t for t in turns if t.outcome == "ANSWERED" and is_tracked(t.concept_id, t.stage) and t.stage]
    return len(answered) >= VALID_MIN_ANSWERS


def room_of(mode: str | None) -> str:
    """The practice room of a session: voice for voice and video, else chat."""
    return "voice" if (mode or "").lower() in {"voice", "video"} else "chat"


def score_overview(session_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare the first and latest non-overlapping groups of valid sessions."""
    scores = [
        float(item["overallScore"])
        for item in session_items
        if item.get("valid") and item.get("overallScore") is not None
    ]
    n = len(scores)
    k = 3 if n >= 6 else 2 if n >= OVERVIEW_MIN_SESSIONS else 0
    latest = round(scores[-1], 1) if scores else None
    if not k:
        return {"sessions": n, "window": 0, "start": None, "recent": None, "change": None, "latest": latest}
    start = statistics.mean(scores[:k])
    recent = statistics.mean(scores[-k:])
    return {
        "sessions": n,
        "window": k,
        "start": round(start, 1),
        "recent": round(recent, 1),
        "change": round(recent - start, 1),
        "latest": latest,
    }


TECHNICAL_FAILURE = "TECHNICAL_FAILURE"


def invalid_reason(session: SessionRow, turns: list[TurnRow]) -> str | None:
    """Return why a session is excluded from progress, or None when it counts."""
    if (session.end_reason or "").upper() == TECHNICAL_FAILURE:
        return "technical_failure"
    if not valid_session(turns):
        return "too_few_answers"
    return None


RETRY_SESSIONS = 30
WINS_LIMIT = 10


def retries_by_session(
    skill_questions: dict[str, list[dict[str, Any]]], names: dict[str, str], areas: dict[str, str]
) -> dict[str, list[dict[str, Any]]]:
    """ "Lần trước – Lần này": a skill asked again after an answer that missed points.

    For each answer whose skill was asked before in another session with key
    points missing: the earlier question and what it missed, this question and
    how much of its checklist it covered. ``sameQuestion`` tells whether it is
    the very same question (then ``fixedPoints`` lists the earlier misses now
    covered); a different question of the same skill is the fairer evidence
    (retest effect, research 24/10), and its points are not matched one by one.
    """
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key, records in skill_questions.items():
        for index, record in enumerate(records):
            earlier = [r for r in records[:index] if r["sessionId"] != record["sessionId"]]
            if not earlier or not earlier[-1]["missing"]:
                continue
            if any(r["skillKey"] == key for r in out.get(record["sessionId"], [])):
                continue  # one card per skill and session: its first question
            before = earlier[-1]
            same = bool(record["question"]) and record["question"] == before["question"]
            now_missing = {m["point"] for m in record["missing"]}
            out[record["sessionId"]].append(
                {
                    "skill": names.get(key) or key,
                    "skillKey": key,
                    "area": areas.get(key, "other"),
                    "sameQuestion": same,
                    "before": {k: before[k] for k in ("sessionId", "date", "question", "score", "missing")},
                    "now": {
                        k: record[k] for k in ("question", "score", "missing", "covered", "total", "checked")
                    },
                    "fixedPoints": [
                        m["point"] for m in before["missing"] if same and m["point"] not in now_missing
                    ],
                }
            )
    keep = sorted(out, key=lambda sid: max(r["before"]["date"] for r in out[sid]))[-RETRY_SESSIONS:]
    return {sid: out[sid] for sid in keep}


BASELINE_SESSIONS = 4
BASELINE_MIN = 3
MATCHED_POINTS = 10
RECORDS_LIMIT = 3


def baseline_label(score: float | None, earlier: list[float], tolerance: float) -> str | None:
    """A session against the candidate's usual level (Oura/WHOOP: against your own baseline).

    The usual level is the median of up to ``BASELINE_SESSIONS`` earlier sessions
    at the same tier; with fewer than ``BASELINE_MIN`` there is no label (no
    verdict on little data, research 24/10).
    """
    recent = earlier[-BASELINE_SESSIONS:]
    if score is None or len(recent) < BASELINE_MIN:
        return None
    usual = statistics.median(recent)
    if score > usual + tolerance:
        return "above"
    if score < usual - tolerance:
        return "below"
    return "within"


def session_timeline(
    session_items: list[dict[str, Any]],
    readiness: list[dict[str, Any]],
    turns_by_session: dict[str, list[TurnRow]],
    calibration: Calibration,
) -> list[dict[str, Any]]:
    """What changed after each session, newest first (Khan: up / same / down after each quiz).

    Per session: the overall score against the usual level, the rounds that
    moved a level, how many kept theirs, and records: things done for the
    first time and a round's best mean at a tier. A measurable STAR result is
    quoted as the evidence.
    """
    rounds_by_job = {job["jobId"]: job["rounds"] for job in readiness}
    tolerance = max(0.3, calibration.typical_error / 2)
    out = []
    for index, item in enumerate(session_items):
        rounds = rounds_by_job.get(item["jobId"] or "", [])
        changes, kept = [], 0
        records: list[dict[str, Any]] = []
        for r in rounds:
            series = r.get("series") or []
            positions = [i for i, p in enumerate(series) if p["sessionId"] == item["sessionId"]]
            if not positions:
                continue
            moved = [e for e in r.get("levelEvents") or [] if e["sessionId"] == item["sessionId"]]
            if moved:
                changes.append({"area": r["area"], "from": moved[0]["from"], "to": moved[-1]["to"]})
            else:
                kept += 1
            point = r["series"][positions[0]]
            before = [p["score"] for p in r["series"][: positions[0]] if p["tier"] == point["tier"]]
            if len(before) >= 2 and point["score"] > max(before):
                records.append(
                    {"kind": "best_round", "area": r["area"], "score": point["score"], "tier": point["tier"]}
                )
        for kind in session_firsts(session_items, index):
            record: dict[str, Any] = {"kind": f"first_{kind}"}
            if kind in ("measurable_result", "star_complete"):
                quote = next(
                    (
                        str(t.star.get("result") or "")[:160]
                        for t in turns_by_session.get(item["sessionId"], [])
                        if t.star and _NUMBER.search(str(t.star.get("result") or ""))
                    ),
                    "",
                )
                if quote:
                    record["quote"] = quote
            records.insert(0, record)
        same_tier = [
            s["overallScore"]
            for s in session_items[:index]
            if s["tier"] == item["tier"] and s["overallScore"] is not None
        ]
        out.append(
            {
                "sessionId": item["sessionId"],
                "date": item["date"],
                "jobId": item["jobId"],
                "tier": item["tier"],
                "score": item["overallScore"],
                "baseline": baseline_label(item["overallScore"], same_tier, tolerance),
                "levelChanges": changes,
                "kept": kept,
                "records": records,
            }
        )
    out.reverse()
    return out


def week_summary(
    timeline: list[dict[str, Any]],
    events: list[dict[str, Any]],
    streak: dict[str, Any],
    *,
    today: date,
    tz_offset_minutes: int,
) -> dict[str, Any]:
    """ "Tuần này" (Garmin training status: one state, one sentence, one advice).

    ``status``: improving (a round moved up or a missed point was made in a
    new question this week), start (no counted session yet), keep_going
    (sessions but no change yet). The page adds the advice from the readiness.
    """
    start = _week_start(today)

    def this_week(iso: str) -> bool:
        return _week_start(_local_day(datetime.fromisoformat(iso), tz_offset_minutes)) == start

    ups = [
        {"area": c["area"], "to": c["to"], "sessionId": t["sessionId"], "jobId": t["jobId"]}
        for t in timeline
        if this_week(t["date"])
        for c in t["levelChanges"]
        if c["to"] > c["from"]
    ]
    fixed = [
        {"point": e["point"], "skill": e["skill"], "jobId": e.get("jobId")}
        for e in events
        if e.get("newQuestion") and this_week(e["date"])
    ]
    if ups or fixed:
        status = "improving"
    elif streak["thisWeekCount"] == 0:
        status = "start"
    else:
        status = "keep_going"
    return {
        "status": status,
        "levelUps": ups,
        "fixed": fixed[:3],
        "sessions": streak["thisWeekCount"],
        "goal": streak["goal"],
        "streakWeeks": streak["weeks"],
    }


def matched_history(
    records: list[dict[str, Any]], tiers: dict[str, str], job_id: str
) -> list[dict[str, Any]]:
    """One skill at one tier over time (Strava matched runs): key points covered per answer.

    The tier is the one of the latest answer for this job; only answers with a
    checklist count.
    """
    mine = [r for r in records if r.get("jobId") == job_id and r.get("total")]
    if not mine:
        return []
    tier = tiers.get(mine[-1]["sessionId"], "standard")
    same = [r for r in mine if tiers.get(r["sessionId"]) == tier][-MATCHED_POINTS:]
    return [
        {
            "sessionId": r["sessionId"],
            "date": r["date"],
            "covered": r["covered"],
            "total": r["total"],
            "score": r["score"],
            "tier": tier,
            "sameQuestion": index > 0 and r["question"] == same[index - 1]["question"],
        }
        for index, r in enumerate(same)
    ]


def build_progress(
    sessions: list[SessionRow],
    turns: list[TurnRow],
    *,
    now: datetime,
    tz_offset_minutes: int = 0,
    weekly_goal: int = DEFAULT_WEEKLY_GOAL,
    all_session_starts: list[datetime] | None = None,
    calibration: Calibration | None = None,
    split_rooms: bool = True,
) -> dict[str, Any]:
    """Build progress views; optionally split comparable metrics by practice room."""
    calibration = calibration or Calibration()
    sessions = sorted(sessions, key=lambda s: s.started_at)
    turns_by_session: dict[str, list[TurnRow]] = defaultdict(list)
    for turn in turns:
        turns_by_session[turn.session_id].append(turn)
    for items in turns_by_session.values():
        items.sort(key=lambda t: t.order)

    session_items = []
    for s in sessions:
        session_items.append(
            {
                "sessionId": s.session_id,
                "date": _iso(s.started_at),
                "jobId": s.job_id,
                "jobTitle": s.job_title,
                "overallScore": _round(s.overall_score),
                "tier": s.tier,
                "difficulty": s.difficulty,
                "resumeId": s.resume_id,
                "resumeName": s.resume_name,
                "graderVersion": s.grader_version,
                "behavior": _session_behavior(turns_by_session.get(s.session_id, [])),
                "valid": invalid_reason(s, turns_by_session.get(s.session_id, [])) is None,
                "invalidReason": invalid_reason(s, turns_by_session.get(s.session_id, [])),
                "mode": s.mode,
                "room": room_of(s.mode),
                "camera": s.mode == "video",
            }
        )
    valid_ids = {item["sessionId"] for item in session_items if item["valid"]}

    # Skills, by canonical concept id: one point per session (mean of its turns).
    # A candidate practises many skills across several CVs and positions, so
    # every point carries its job and CV for the page filters. A question
    # without a concept id is keyed by its normalized label, not dropped.
    skill_points: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skill_labels: dict[str, str] = {}
    skill_concepts: dict[str, str | None] = {}
    # The questions behind each skill, newest last: what was asked, the score,
    # and the key points the answer missed (from the grading checklist).
    skill_questions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in sessions:
        per_concept: dict[str, list[float]] = defaultdict(list)
        difficulty: dict[str, str | None] = {}
        for t in turns_by_session.get(s.session_id, []):
            key = skill_key(t.concept_id, t.label)
            # The greeting and the closing Q&A are part of the interview, not skills.
            if not key or t.score is None or t.outcome != "ANSWERED" or not is_tracked(t.concept_id, t.stage):
                continue
            per_concept[key].append(float(t.score))
            difficulty.setdefault(key, t.difficulty)
            skill_concepts[key] = t.concept_id
            if t.label:
                skill_labels[key] = t.label  # latest label wins
            states = _point_states(t)
            skill_questions[key].append(
                {
                    "sessionId": s.session_id,
                    "date": _iso(s.started_at),
                    "question": t.question[:240],
                    "score": round(float(t.score), 1),
                    "missing": _missing_points(t),
                    "checked": _checked(t),
                    "covered": sum(1 for _, status in states if status == "COVERED"),
                    "total": len(states),
                    "jobId": s.job_id,
                }
            )
        for concept_id, scores in per_concept.items():
            skill_points[concept_id].append(
                {
                    "sessionId": s.session_id,
                    "date": _iso(s.started_at),
                    "score": round(statistics.mean(scores), 1),
                    "difficulty": difficulty.get(concept_id),
                    "tier": s.tier,
                    "jobId": s.job_id,
                    "resumeId": s.resume_id,
                    "graderVersion": s.grader_version,
                    "valid": s.session_id in valid_ids,
                }
            )
    skills = []
    for key, points in skill_points.items():
        name = display_name(skill_concepts.get(key), skill_labels.get(key) or key)
        view = _skill_view(key, name, points, calibration)
        view["area"] = area_of(skill_concepts.get(key))
        view["taxonomyLabel"] = skill_labels.get(key)
        view["recentQuestions"] = list(reversed(skill_questions[key][-RECENT_QUESTIONS:]))
        # No longer asked as a topic of its own (interview_rounds.MINOR_CONCEPTS).
        view["minor"] = is_minor(skill_concepts.get(key))
        skills.append(view)
    skills.sort(key=lambda item: (-len(item["points"]), item["label"].casefold()))

    # Jobs: overall score per session of the same job, with the tier.
    job_points: dict[str, list[dict[str, Any]]] = defaultdict(list)
    job_titles: dict[str, str | None] = {}
    for item in session_items:
        if not item["jobId"] or item["overallScore"] is None:
            continue
        job_points[item["jobId"]].append(
            {
                "sessionId": item["sessionId"],
                "date": item["date"],
                "score": item["overallScore"],
                "tier": item["tier"],
            }
        )
        job_titles[item["jobId"]] = item["jobTitle"]
    jobs = []
    for job_id, points in job_points.items():
        tier_changes = [
            {"sessionId": cur["sessionId"], "date": cur["date"], "from": prev["tier"], "to": cur["tier"]}
            for prev, cur in zip(points, points[1:])
            if _TIER_RANK.get(cur["tier"], 1) != _TIER_RANK.get(prev["tier"], 1)
        ]
        jobs.append(
            {
                "jobId": job_id,
                "jobTitle": job_titles.get(job_id),
                "points": points,
                "tierChanges": tier_changes,
                "currentTier": points[-1]["tier"],
                # A trend is only read within one tier: a harder tier lowers raw scores.
                "trend": reliable_trend(
                    [p["score"] for p in points if p["tier"] == points[-1]["tier"]], calibration
                ),
            }
        )
    jobs.sort(key=lambda item: item["points"][-1]["date"], reverse=True)  # most recently practised first

    # Gaps across every session, kept long enough for the readiness rounds.
    names = {s["conceptId"]: s["label"] for s in skills}
    all_gaps = gap_notebook(sessions, turns_by_session, names, limited=False, now=now)
    # Points due for practice first, then the most often missed.
    open_sorted = sorted(all_gaps["open"], key=lambda item: not item.get("due"))
    gaps = {"open": open_sorted[:GAP_OPEN_LIMIT], "fixed": all_gaps["fixed"][:GAP_FIXED_LIMIT]}
    retries = retries_by_session(
        skill_questions, names, {key: area_of(skill_concepts.get(key)) for key in skill_questions}
    )
    wins = small_wins(session_items, all_gaps["events"])

    rates = [
        i["behavior"]["firstTimeSufficientRate"]
        for i in session_items
        if i["behavior"]["firstTimeSufficientRate"] is not None
    ]
    behavior = {
        "firstTimeSufficientRate": {
            "latest": rates[-1] if rates else None,
            # Rates are 0–1: scale the flat band to the same 0.5-in-10 resolution.
            "trend": _rate_trend(rates),
        },
        "skippedPerSession": {
            "latest": session_items[-1]["behavior"]["skipped"] if session_items else None,
            "trend": _inverse(trend([float(i["behavior"]["skipped"]) for i in session_items])),
        },
    }

    starts = all_session_starts if all_session_starts is not None else [s.started_at for s in sessions]
    today = _local_day(now, tz_offset_minutes)
    valid_days = [
        _local_day(s.started_at, tz_offset_minutes)
        for s, item in zip(sessions, session_items, strict=True)
        if item["valid"]
    ]
    fixed_days = [
        _local_day(datetime.fromisoformat(e["date"]), tz_offset_minutes) for e in all_gaps["events"]
    ]
    streak = weekly_streak(valid_days, today=today, goal=weekly_goal, progress_days=fixed_days)
    started_days = [_local_day(moment, tz_offset_minutes) for moment in starts]
    streak["startedThisWeek"] = sum(1 for day in started_days if _week_start(day) == _week_start(today))
    streak["validMinAnswers"] = VALID_MIN_ANSWERS

    resumes: dict[str, dict[str, Any]] = {}
    for item in session_items:
        if item["resumeId"]:
            entry = resumes.setdefault(
                item["resumeId"], {"resumeId": item["resumeId"], "name": item["resumeName"], "sessions": 0}
            )
            entry["sessions"] += 1
            entry["name"] = item["resumeName"] or entry["name"]

    # Readiness per position by round, with the level ladder (research 24/10).
    tiers = {item["sessionId"]: item["tier"] for item in session_items}
    readiness = []
    for job in jobs:
        rounds = round_readiness(skills, all_gaps["open"], job["jobId"], calibration)
        for r in rounds:
            for skill in r["skills"]:
                answers = skill_questions.get(skill["conceptId"], [])
                skill["history"] = matched_history(answers, tiers, job["jobId"])
        readiness.append(
            {
                "jobId": job["jobId"],
                "jobTitle": job["jobTitle"],
                "sessions": len(job["points"]),
                "rounds": rounds,
                "readyCount": sum(1 for r in rounds if r["level"] >= READY_LEVEL_INDEX),
                "readyHistory": ready_history(rounds, today=today, tz_offset_minutes=tz_offset_minutes),
                "currentTier": job["currentTier"],
            }
        )
    timeline = session_timeline(session_items, readiness, turns_by_session, calibration)

    result = {
        "generatedAt": _iso(now),
        "weeklyGoal": weekly_goal,
        "resumes": sorted(resumes.values(), key=lambda r: -r["sessions"]),
        "streak": streak,
        "activity": activity_days(started_days, valid_days, fixed_days, today=today),
        "wins": wins,
        "retries": retries,
        "sessions": session_items,
        "skills": skills,
        "gaps": gaps,
        "readiness": readiness,
        "timeline": timeline,
        "week": week_summary(
            timeline, all_gaps["events"], streak, today=today, tz_offset_minutes=tz_offset_minutes
        ),
        "jobs": jobs,
        "behavior": behavior,
        "milestones": milestones(session_items, skills),
        "trendRule": {
            "minPoints": TREND_MIN_POINTS,
            "window": TREND_WINDOW,
            "flatDelta": FLAT_DELTA,
            "probability": TREND_PROBABILITY,
            "levelEdges": list(LEVEL_EDGES),
            "levelMargin": LEVEL_MARGIN,
            "processSd": PROCESS_SD,
        },
        "calibration": calibration.to_public(),
        "overview": score_overview(session_items),
    }
    if split_rooms:
        rooms: dict[str, Any] = {}
        for room in ROOMS:
            room_sessions = [s for s in sessions if room_of(s.mode) == room]
            ids = {s.session_id for s in room_sessions}
            sub = build_progress(
                room_sessions,
                [t for t in turns if t.session_id in ids],
                now=now,
                tz_offset_minutes=tz_offset_minutes,
                weekly_goal=weekly_goal,
                calibration=calibration,
                split_rooms=False,
            )
            view = {key: sub[key] for key in ROOM_KEYS}
            view["count"] = len(ids)
            view["cameraSessions"] = sum(1 for s in room_sessions if s.mode == "video")
            rooms[room] = view
        result["rooms"] = rooms
    return result


def _skill_view(
    concept_id: str, label: str, points: list[dict[str, Any]], calibration: Calibration
) -> dict[str, Any]:
    """A skill's points with its noise-aware trend, smoothed estimate and level."""
    comparable = _comparable(points, calibration)
    values = [_adjusted(p, calibration) for p in comparable]
    estimates = kalman_estimates(values, calibration)
    level, changes = skill_levels([mean for mean, _ in estimates])
    by_session = {p["sessionId"]: estimate for p, estimate in zip(comparable, estimates, strict=True)}
    for point in points:
        estimate = by_session.get(point["sessionId"])
        point["estimate"] = estimate[0] if estimate else None
        point["estimateSd"] = estimate[1] if estimate else None
    return {
        "conceptId": concept_id,
        "label": label,
        "points": points,
        "latest": points[-1]["score"],
        "best": max(p["score"] for p in points),
        "trend": reliable_trend(values, calibration),
        "comparablePoints": len(comparable),
        "estimate": estimates[-1][0] if estimates else None,
        "estimateSd": estimates[-1][1] if estimates else None,
        "level": level,
        "levelEvents": [
            {
                "sessionId": comparable[index]["sessionId"],
                "date": comparable[index]["date"],
                "from": before,
                "to": after,
            }
            for index, before, after in changes
        ],
    }


def _rate_trend(rates: list[float]) -> dict[str, Any]:
    result = trend([rate * 10 for rate in rates])
    if result["delta"] is not None:
        result["delta"] = round(result["delta"] / 10, 2)
    return result


def _inverse(result: dict[str, Any]) -> dict[str, Any]:
    """For counts where fewer is better: "up" means improving."""
    status = {"up": "down", "down": "up"}.get(result["status"], result["status"])
    return {**result, "status": status}


def milestones(session_items: list[dict[str, Any]], skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if session_items:
        first = session_items[0]
        out.append({"kind": "first_session", "date": first["date"], "sessionId": first["sessionId"]})
    seen_challenge = False
    seen_clean = False
    best_tier_by_job: dict[str, int] = {}
    for item in session_items:
        if item["tier"] == "challenge" and not seen_challenge:
            seen_challenge = True
            out.append(
                {
                    "kind": "first_challenge",
                    "date": item["date"],
                    "sessionId": item["sessionId"],
                    "jobTitle": item["jobTitle"],
                }
            )
        rank = _TIER_RANK.get(item["tier"], 1)
        job = item["jobId"] or ""
        if job in best_tier_by_job and rank > best_tier_by_job[job] and item["tier"] != "challenge":
            out.append(
                {
                    "kind": "tier_up",
                    "date": item["date"],
                    "sessionId": item["sessionId"],
                    "jobTitle": item["jobTitle"],
                    "tier": item["tier"],
                }
            )
        best_tier_by_job[job] = max(rank, best_tier_by_job.get(job, rank))
        behavior = item["behavior"]
        if not seen_clean and behavior["technicalTurns"] >= 3 and behavior["skipped"] == 0:
            seen_clean = True
            out.append({"kind": "no_skip_session", "date": item["date"], "sessionId": item["sessionId"]})
    # A skill's milestone is a level up of its smoothed estimate. A single
    # session score passing 7/10 is not one: it was often noise (20/10).
    for skill in skills:
        for event in skill.get("levelEvents") or []:
            if event["to"] > event["from"]:
                out.append(
                    {
                        "kind": "skill_level_up",
                        "date": event["date"],
                        "sessionId": event["sessionId"],
                        "skill": skill["label"],
                        "level": event["to"],
                    }
                )
    out.sort(key=lambda m: m["date"])
    return out


def compare_with_previous(progress: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    """ "So với lần trước" cho báo cáo cuối buổi.

    Lần trước là buổi đã chấm gần nhất **cùng vị trí**; không có thì buổi gần nhất
    bất kỳ (khi đó chỉ so kỹ năng và cách trả lời, không so điểm tổng).
    """
    items = progress["sessions"]
    index = next((i for i, s in enumerate(items) if s["sessionId"] == session_id), None)
    if index is None or index == 0:
        return None
    current = items[index]
    earlier = items[:index]
    same_job = [s for s in earlier if s["jobId"] and s["jobId"] == current["jobId"]]
    previous = (same_job or earlier)[-1]
    is_same_job = bool(same_job)

    # One session against one: only a change beyond the typical error of a
    # session reads as a change (a 0.5 band flagged noise as progress).
    single_delta = float((progress.get("calibration") or {}).get("typicalError") or FLAT_DELTA)
    improved, focus = [], []
    for skill in progress["skills"]:
        by_session = {p["sessionId"]: p for p in skill["points"]}
        now_point = by_session.get(session_id)
        if not now_point:
            continue
        before = [p for p in skill["points"] if p["date"] < now_point["date"]]
        if not before:
            continue
        delta = now_point["score"] - before[-1]["score"]
        entry = {
            "skill": skill["label"],
            "before": before[-1]["score"],
            "now": now_point["score"],
            "delta": round(delta, 1),
        }
        if delta >= single_delta:
            improved.append(entry)
        elif delta <= -single_delta:
            focus.append(entry)
    improved.sort(key=lambda e: -e["delta"])
    focus.sort(key=lambda e: e["delta"])
    if not focus:
        weakest = sorted(
            (
                {"skill": s["label"], "now": p["score"]}
                for s in progress["skills"]
                for p in s["points"]
                if p["sessionId"] == session_id
            ),
            key=lambda e: e["now"],
        )
        focus = weakest[:1]

    overall = None
    if is_same_job and current["overallScore"] is not None and previous["overallScore"] is not None:
        delta = current["overallScore"] - previous["overallScore"]
        overall = {
            "before": previous["overallScore"],
            "now": current["overallScore"],
            "delta": round(delta, 1),
            # Different tiers: the raw scores are not comparable.
            "comparable": current["tier"] == previous["tier"],
            "status": "flat" if abs(delta) < single_delta else ("up" if delta > 0 else "down"),
        }
    cur_b, prev_b = current["behavior"], previous["behavior"]
    level_changes = [
        {"skill": skill["label"], "from": event["from"], "to": event["to"]}
        for skill in progress["skills"]
        for event in skill.get("levelEvents") or []
        if event["sessionId"] == session_id
    ]
    return {
        "sessionId": session_id,
        "previousSessionId": previous["sessionId"],
        "previousDate": previous["date"],
        "sameJob": is_same_job,
        "tier": {"before": previous["tier"], "now": current["tier"]},
        "overall": overall,
        "improvedSkills": improved[:3],
        "focusSkills": focus[:1],
        "behavior": {
            "firstTimeSufficientRate": {
                "before": prev_b["firstTimeSufficientRate"],
                "now": cur_b["firstTimeSufficientRate"],
            },
            "skipped": {"before": prev_b["skipped"], "now": cur_b["skipped"]},
            "coreCoverage": {"before": _coverage(prev_b), "now": _coverage(cur_b)},
        },
        "levelChanges": level_changes,
        "firsts": session_firsts(items, index),
        # "Lần trước – Lần này": skills asked again after an answer that missed points.
        "retries": (progress.get("retries") or {}).get(session_id, [])[:3],
        # "Sau buổi này": rounds that moved a level, usual level, records.
        "after": next((t for t in progress.get("timeline") or [] if t["sessionId"] == session_id), None),
    }


def _coverage(behavior: dict[str, Any]) -> float | None:
    total = behavior.get("coreTotal") or 0
    return round(behavior.get("coreCovered", 0) / total, 2) if total else None


# What a session did for the first time, from its counts (the session recap card).
_FIRSTS = (
    ("full_core", "fullCoreTurns"),
    ("star_complete", "starComplete"),
    ("measurable_result", "measurableResult"),
    ("recovered_after_probe", "recoveredAfterProbe"),
)


def session_firsts(items: list[dict[str, Any]], index: int) -> list[str]:
    """Kinds of answer this session produced for the first time across the candidate's sessions."""
    if index == 0:
        return []
    current = items[index]["behavior"]
    earlier = [item["behavior"] for item in items[:index]]
    out = [kind for kind, key in _FIRSTS if current.get(key) and not any(b.get(key) for b in earlier)]
    if (
        current.get("checklistTurns", 0) >= 3
        and not current.get("incorrectTurns")
        and all(b.get("incorrectTurns") for b in earlier if b.get("checklistTurns", 0) >= 3)
        and any(b.get("checklistTurns", 0) >= 3 for b in earlier)
    ):
        out.append("no_incorrect")
    return out


# --------------------------------------------------------------------------- #
# Đọc dữ liệu
# --------------------------------------------------------------------------- #


def small_wins(session_items: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ "Ghi nhận nhỏ": concrete things done for the first time, newest first.

    A missed point covered in a *different* question of the same skill, and the
    first answer of a kind (all key points, STAR complete, a measurable
    result, completing after a follow-up). The progress principle (Amabile &
    Kramer): a specific small win motivates more than a score (research 24/10).
    A point covered again in the same question is left out: it can be memory.
    """
    out: list[dict[str, Any]] = [
        {
            "kind": "fixed",
            "date": e["date"],
            "sessionId": e["sessionId"],
            "point": e["point"],
            "skill": e["skill"],
            "area": e["area"],
            "jobId": e.get("jobId"),
        }
        for e in events
        if e["newQuestion"]
    ]
    for index, item in enumerate(session_items):
        for kind in session_firsts(session_items, index):
            out.append(
                {
                    "kind": f"first_{kind}",
                    "date": item["date"],
                    "sessionId": item["sessionId"],
                    "jobId": item["jobId"],
                }
            )
    out.sort(key=lambda win: win["date"], reverse=True)
    return out[:WINS_LIMIT]


__all__ = [
    "_session_behavior",
    "valid_session",
    "room_of",
    "score_overview",
    "TECHNICAL_FAILURE",
    "invalid_reason",
    "RETRY_SESSIONS",
    "WINS_LIMIT",
    "retries_by_session",
    "BASELINE_SESSIONS",
    "BASELINE_MIN",
    "MATCHED_POINTS",
    "RECORDS_LIMIT",
    "baseline_label",
    "session_timeline",
    "week_summary",
    "matched_history",
    "build_progress",
    "_skill_view",
    "_rate_trend",
    "_inverse",
    "milestones",
    "compare_with_previous",
    "_coverage",
    "_FIRSTS",
    "session_firsts",
    "small_wins",
]
