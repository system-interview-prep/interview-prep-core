"""Tiến bộ của ứng viên qua các buổi phỏng vấn.

Mọi thứ ở đây tính lại từ dữ liệu đã lưu (buổi, kế hoạch, đánh giá từng câu, tin
nhắn), không gọi AI. Nguyên tắc (nghiên cứu ngày 19/10,
``DOC_AND_PLAN/plan/interview/PRACTICE_PROGRESS_2026-10-19.md``):

* **So những gì so được.** Điểm tổng của hai buổi khác vị trí hoặc khác mức luyện
  không so trực tiếp được. Vì vậy tiến bộ được tính theo:
  - **kỹ năng**: điểm từng câu, gom theo mã kỹ năng chuẩn (``conceptId`` trong câu
    hỏi đã khoá), không theo tên hiển thị của JD;
  - **vị trí**: điểm tổng các buổi cùng một vị trí, kèm mức luyện của từng buổi;
    lên mức là một mốc tiến bộ, không phải tụt điểm;
  - **cách trả lời**: tỉ lệ câu đủ ý ngay lần đầu, số lần phải hỏi thêm, số câu
    bỏ qua; không phụ thuộc vị trí.
* **Chống hiểu nhầm do điểm dao động** (nghiên cứu 20/10, PROGRESS_EVIDENCE_RESEARCH).
  Xu hướng cần đủ 3 điểm; so trung bình tối đa 3 lần gần nhất với 3 lần đầu, và chỉ
  gọi là "đang tốt lên" khi xác suất thay đổi thật vượt "thay đổi nhỏ nhất có ý
  nghĩa" từ 80% trở lên, tính từ độ nhiễu đo được (``progress_calibration``). Chỉ so
  trong cùng mức luyện (hoặc sau khi quy đổi theo độ chênh giữa các mức đã đo) và
  trong cùng phiên bản cách chấm.
* **Bằng chứng cụ thể hơn con số.** Mỗi buổi kèm số đếm từ checklist chấm điểm:
  ý chính đã nêu, câu có ý sai, câu đủ STAR, câu có kết quả đo được, câu phải hỏi
  thêm và câu bổ sung đủ sau khi được hỏi thêm. Trang so các tỉ lệ này giữa những
  câu đầu và những câu gần nhất.
* **Cấp độ có độ trễ.** Mỗi kỹ năng có một đường ước lượng làm mượt (bộ lọc Kalman)
  và một cấp độ có mô tả; cấp chỉ đổi khi ước lượng vượt ngưỡng một khoảng, để một
  buổi kém không làm tụt cấp.
* **Chuỗi theo tuần, không theo ngày.** Một buổi dài khoảng 25 phút, nên chuỗi ngày
  quá nặng và dễ đứt (đứt chuỗi làm mất động lực). Đạt mục tiêu tuần thì chuỗi
  +1 tuần; mỗi tháng có một lần giữ chuỗi khi lỡ một tuần.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.interviews.application.progress_calibration import Calibration, load_calibration
from src.modules.interviews.application.skill_display import area_of, display_name, is_tracked
from src.modules.interviews.core.interview_rounds import AREAS, is_minor

DEFAULT_WEEKLY_GOAL = 2
WEEKLY_GOAL_RANGE = (1, 7)
TREND_MIN_POINTS = 3
TREND_WINDOW = 3
FLAT_DELTA = 0.5
STREAK_HISTORY_WEEKS = 8
# Computed on request from raw rows. About 10 graded turns per session, so 1000
# sessions is about 10k rows: fine today. Past that, keep a per-session summary
# row written when the evaluation is saved (see PRACTICE_PROGRESS doc, section 7).
MAX_SESSIONS = 1000
_TIER_RANK = {"basic": 0, "standard": 1, "challenge": 2}
_TECHNICAL_STAGES = {"VALIDATE", "DEEP_DIVE", "CHALLENGE"}
# Experience questions, where a STAR answer (situation, task, action, result) is expected.
_STAR_STAGES = {"VALIDATE", "BEHAVIORAL"}
# "Đang tốt lên" only when the chance of a real change beyond the smallest
# worthwhile change is at least this (Hopkins: 80% for individual decisions).
TREND_PROBABILITY = 0.8
# A probed answer counts as recovered when this share of its core points ended up covered.
RECOVERED_COVERAGE = 0.75
# Skill levels: lower edges of levels 1..4 on the 0-10 scale; level 0 below the first.
LEVEL_EDGES = (4.0, 5.5, 7.0, 8.5)
# The estimate must pass an edge by this much to change level (hysteresis).
LEVEL_MARGIN = 0.3
# How much a skill can really change between two sessions (Kalman process noise, SD).
PROCESS_SD = 0.35
_NUMBER = re.compile(r"\d")


@dataclass
class SessionRow:
    session_id: str
    job_id: str | None
    job_title: str | None
    started_at: datetime
    overall_score: float | None
    tier: str = "standard"
    difficulty: str | None = None
    resume_id: str | None = None
    resume_name: str | None = None
    grader_version: str | None = None


@dataclass
class TurnRow:
    session_id: str
    stage: str
    concept_id: str | None
    label: str | None
    difficulty: str | None
    score: float | None
    outcome: str
    needed_probe: bool = False
    order: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
    # Checklist verdicts {key, importance, status, quote}; empty for holistic grading.
    rubric_points: list[dict[str, Any]] = field(default_factory=list)
    # STAR analysis of the grader: {situation, task, action, result, is_star_complete}.
    star: dict[str, Any] = field(default_factory=dict)
    question: str = ""


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def reliable_trend(values: list[float], calibration: Calibration) -> dict[str, Any]:
    """Trend of a skill's session scores that accounts for their noise.

    Compares the mean of the latest ``w`` points with the first ``w`` (w = up to
    3). The change counts as up (down) when the probability that the true change
    exceeds the smallest worthwhile change is at least ``TREND_PROBABILITY``,
    given the measured typical error of one session (Hopkins).
    """
    if len(values) < TREND_MIN_POINTS:
        return {"status": "insufficient", "delta": None, "probability": None}
    window = min(TREND_WINDOW, len(values) // 2 or 1)
    delta = statistics.mean(values[-window:]) - statistics.mean(values[:window])
    se = calibration.typical_error * math.sqrt(2.0 / window)
    swc = calibration.swc
    p_up = _phi((delta - swc) / se)
    p_down = _phi((-delta - swc) / se)
    status = "up" if p_up >= TREND_PROBABILITY else "down" if p_down >= TREND_PROBABILITY else "flat"
    return {
        "status": status,
        "delta": round(delta, 1),
        "probability": round(p_up if delta >= 0 else p_down, 2),
    }


def kalman_estimates(values: list[float], calibration: Calibration) -> list[tuple[float, float]]:
    """(estimate, sd) after each point: a local-level filter, started at the first point.

    Observation noise is the typical error of one session; the skill may drift
    by ``PROCESS_SD`` between sessions. The first point sets the start, so the
    line is not pulled toward a cohort mean the candidate has never been near.
    """
    r = calibration.typical_error**2
    q = PROCESS_SD**2
    out: list[tuple[float, float]] = []
    mean, var = 0.0, 0.0
    for index, value in enumerate(values):
        if index == 0:
            mean, var = value, r
        else:
            var += q
            gain = var / (var + r)
            mean += gain * (value - mean)
            var *= 1 - gain
        out.append((round(mean, 2), round(math.sqrt(var), 2)))
    return out


def _band(value: float) -> int:
    return sum(1 for edge in LEVEL_EDGES if value >= edge)


def skill_levels(estimates: list[float]) -> tuple[int, list[tuple[int, int, int]]]:
    """Current level and (index, from, to) changes, with hysteresis around the edges."""
    if not estimates:
        return 0, []
    level = _band(estimates[0])
    changes: list[tuple[int, int, int]] = []
    for index, value in enumerate(estimates[1:], start=1):
        new = level
        while new < len(LEVEL_EDGES) and value >= LEVEL_EDGES[new] + LEVEL_MARGIN:
            new += 1
        while new > 0 and value < LEVEL_EDGES[new - 1] - LEVEL_MARGIN:
            new -= 1
        if new != level:
            changes.append((index, level, new))
            level = new
    return level, changes


def _comparable(points: list[dict[str, Any]], calibration: Calibration) -> list[dict[str, Any]]:
    """Points that can be compared with the latest one.

    Same practice tier unless tier offsets were measured (then scores are moved
    onto the at-level scale), and the same grader version (no version recorded
    means an older evaluation and is kept).
    """
    if not points:
        return []
    latest = points[-1]
    versions = [p.get("graderVersion") for p in points if p.get("graderVersion")]
    current_version = versions[-1] if versions else None
    out = []
    for point in points:
        if current_version and point.get("graderVersion") and point["graderVersion"] != current_version:
            continue
        if not calibration.tier_adjusted and point.get("tier") != latest.get("tier"):
            continue
        out.append(point)
    return out


def _adjusted(point: dict[str, Any], calibration: Calibration) -> float:
    if not calibration.tier_adjusted:
        return float(point["score"])
    return float(point["score"]) + calibration.offset(point.get("tier") or "standard")


def trend(values: list[float]) -> dict[str, Any]:
    """{status, delta}: up / down / flat, or insufficient below 3 points.

    Fixed 0.5 band, kept for counts and rates (skipped questions, first-time
    sufficient rate); scores use ``reliable_trend``.
    """
    if len(values) < TREND_MIN_POINTS:
        return {"status": "insufficient", "delta": None}
    window = min(TREND_WINDOW, len(values) // 2 or 1)
    first = statistics.mean(values[:window])
    last = statistics.mean(values[-window:])
    delta = last - first
    status = "flat" if abs(delta) < FLAT_DELTA else ("up" if delta > 0 else "down")
    return {"status": status, "delta": round(delta, 1)}


# --------------------------------------------------------------------------- #
# Chuỗi tuần
# --------------------------------------------------------------------------- #


def _local_day(moment: datetime, tz_offset_minutes: int) -> date:
    return (moment.astimezone(UTC) + timedelta(minutes=tz_offset_minutes)).date()


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())  # Monday


def weekly_streak(
    session_days: list[date], *, today: date, goal: int
) -> dict[str, Any]:
    """Tuần liên tiếp đạt mục tiêu, tính đến tuần này.

    Tuần này chưa đạt thì chưa tính là đứt (tuần chưa hết). Một tuần đã qua không
    đạt được "giữ chuỗi" nếu tháng của tuần đó chưa dùng lượt giữ nào; khi đó
    chuỗi không đứt nhưng cũng không cộng thêm tuần đó.
    """
    counts: dict[date, int] = defaultdict(int)
    for day in session_days:
        counts[_week_start(day)] += 1
    current = _week_start(today)
    this_week = counts.get(current, 0)
    streak = 1 if this_week >= goal else 0
    freezes_used: set[tuple[int, int]] = set()
    history: list[dict[str, Any]] = [
        {"weekStart": current.isoformat(), "count": this_week, "met": this_week >= goal, "frozen": False, "current": True}
    ]
    week = current - timedelta(days=7)
    first_week = min(counts) if counts else current
    broken = False
    while week >= first_week:
        count = counts.get(week, 0)
        met = count >= goal
        frozen = False
        if not broken:
            if met:
                streak += 1
            elif (week.year, week.month) not in freezes_used:
                freezes_used.add((week.year, week.month))
                frozen = True
            else:
                broken = True
        if len(history) < STREAK_HISTORY_WEEKS:
            history.append(
                {"weekStart": week.isoformat(), "count": count, "met": met, "frozen": frozen, "current": False}
            )
        if broken and len(history) >= STREAK_HISTORY_WEEKS:
            break
        week -= timedelta(days=7)
    return {
        "weeks": streak,
        "goal": goal,
        "thisWeekCount": this_week,
        "thisWeekRemaining": max(0, goal - this_week),
        "freezeAvailableThisMonth": (today.year, today.month) not in freezes_used,
        "history": list(reversed(history)),
    }


# --------------------------------------------------------------------------- #
# Tiến bộ
# --------------------------------------------------------------------------- #


def skill_key(concept_id: str | None, label: str | None) -> str | None:
    if concept_id:
        return str(concept_id)
    normalized = " ".join(str(label or "").casefold().split())
    return f"label:{normalized}" if normalized else None


def _core_coverage(turn: TurnRow) -> tuple[float, int, bool, bool]:
    """(covered core points, core points, every core point covered, any point stated wrongly)."""
    core = [p for p in turn.rubric_points if str(p.get("importance") or "CORE").upper() != "BONUS"]
    covered = sum(
        1.0 if p.get("status") == "COVERED" else 0.5 if p.get("status") == "PARTIAL" else 0.0 for p in core
    )
    full = bool(core) and all(p.get("status") == "COVERED" for p in core)
    incorrect = any(p.get("status") == "INCORRECT" for p in turn.rubric_points)
    return covered, len(core), full, incorrect


# Questions listed under each skill, and missed points listed per question.
RECENT_QUESTIONS = 3
MISSING_POINTS = 3


_STAR_PARTS = (
    ("situation", "Bối cảnh (chuyện xảy ra ở đâu, khi nào)"),
    ("task", "Nhiệm vụ của bạn"),
    ("action", "Bạn đã làm gì cụ thể"),
    ("result", "Kết quả (tốt nhất có số liệu)"),
)


GAP_OPEN_LIMIT = 12
GAP_FIXED_LIMIT = 6


def _point_states(turn: TurnRow) -> list[tuple[str, str]]:
    """(point, status) for every core point the grader judged on this answer."""
    if not turn.rubric_points and turn.star:
        return [
            (name, "COVERED" if str(turn.star.get(key) or "").strip() else "MISSING") for key, name in _STAR_PARTS
        ]
    out = []
    for point in turn.rubric_points:
        if str(point.get("importance") or "CORE").upper() == "BONUS" or not point.get("description"):
            continue
        out.append((str(point["description"])[:200], str(point.get("status") or "MISSING")))
    return out


def gap_notebook(
    sessions: list[SessionRow],
    turns_by_session: dict[str, list[TurnRow]],
    names: dict[str, str],
    *,
    limited: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Key points the candidate keeps leaving out, and the ones they fixed.

    The research (20/10) found the question candidates ask before a real
    interview is "what will they ask and where am I still weak", which a 0-10
    score per keyword does not answer. A point is followed across sessions by
    its text within one skill (the same question, or STAR part, asked again):

    * ``open``: missed or partly covered the last time it was judged.
    * ``fixed``: missed at least once before and covered the last time.

    Job keywords that are no longer asked on their own (JSON, XML) are left
    out; so are the greeting and the closing Q&A.
    """
    state: dict[tuple[str, str], dict[str, Any]] = {}
    for s in sorted(sessions, key=lambda item: item.started_at):
        for t in turns_by_session.get(s.session_id, []):
            key = skill_key(t.concept_id, t.label)
            if not key or t.outcome != "ANSWERED" or is_minor(t.concept_id):
                continue
            if not is_tracked(t.concept_id, t.stage):
                continue
            for point, status in _point_states(t):
                entry = state.setdefault(
                    (key, point.casefold()),
                    {
                        "skill": names.get(key) or key,
                        "skillKey": key,
                        "area": area_of(t.concept_id),
                        "point": point,
                        "timesAsked": 0,
                        "timesMissed": 0,
                    },
                )
                entry["timesAsked"] += 1
                entry["status"] = status
                entry["lastSeen"] = {
                    "sessionId": s.session_id,
                    "date": _iso(s.started_at),
                    "question": t.question[:240],
                    "jobId": s.job_id,
                    "resumeId": s.resume_id,
                }
                if status != "COVERED":
                    entry["timesMissed"] += 1
    items = list(state.values())
    still_open = [item for item in items if item["status"] != "COVERED"]
    fixed = [item for item in items if item["status"] == "COVERED" and item["timesMissed"] > 0]
    # Most often missed first; among equals, the most recent (stable sorts).
    still_open.sort(key=lambda item: item["lastSeen"]["date"], reverse=True)
    still_open.sort(key=lambda item: -item["timesMissed"])
    fixed.sort(key=lambda item: item["lastSeen"]["date"], reverse=True)
    if not limited:
        return {"open": still_open, "fixed": fixed}
    return {"open": still_open[:GAP_OPEN_LIMIT], "fixed": fixed[:GAP_FIXED_LIMIT]}


# A round is "ready" when the smoothed level is at Vững (LEVEL_EDGES[2]) and
# rests on more than one session: one interview says little (interviewing.io:
# only about a quarter of people perform consistently between interviews).
ROUND_READY = 7.0
ROUND_ALMOST = 5.5
ROUND_MIN_SESSIONS = 2
# Asked in every session (VALIDATE and BEHAVIORAL presets).
ALWAYS_ASKED = ("project", "teamwork")


def round_readiness(
    skills: list[dict[str, Any]], gaps: list[dict[str, Any]], job_id: str
) -> list[dict[str, Any]]:
    """Readiness for one position, by interview round (interview_rounds.AREAS).

    Per round: the mean smoothed level of its skills as asked for this job,
    the number of sessions behind it, a status (ready / almost / practice /
    thin = one session only / unseen), and the point missed most often there.
    """
    per_area: dict[str, dict[str, Any]] = {}
    for skill in skills:
        if skill.get("minor"):
            continue
        points = [p for p in skill["points"] if p.get("jobId") == job_id]
        if not points:
            continue
        last = points[-1]
        level = last.get("estimate") if last.get("estimate") is not None else last["score"]
        entry = per_area.setdefault(skill["area"], {"skills": [], "sessions": set()})
        entry["skills"].append(
            {"conceptId": skill["conceptId"], "label": skill["label"], "estimate": round(level, 1)}
        )
        entry["sessions"].update(p["sessionId"] for p in points)
    rounds = []
    for area in AREAS:
        entry = per_area.get(area)
        if entry is None and area not in ALWAYS_ASKED:
            continue
        top_gap = next(
            (g for g in gaps if g["area"] == area and g["lastSeen"].get("jobId") == job_id), None
        )
        if entry is None:
            rounds.append(
                {
                    "area": area,
                    "status": "unseen",
                    "estimate": None,
                    "sessions": 0,
                    "skills": [],
                    "topGap": None,
                }
            )
            continue
        estimate = round(statistics.mean(item["estimate"] for item in entry["skills"]), 1)
        sessions = len(entry["sessions"])
        if sessions < ROUND_MIN_SESSIONS:
            status = "thin"
        elif estimate >= ROUND_READY:
            status = "ready"
        elif estimate >= ROUND_ALMOST:
            status = "almost"
        else:
            status = "practice"
        rounds.append(
            {
                "area": area,
                "status": status,
                "estimate": estimate,
                "sessions": sessions,
                "skills": sorted(entry["skills"], key=lambda item: item["estimate"]),
                "topGap": (
                    {
                        "point": top_gap["point"],
                        "skill": top_gap["skill"],
                        "timesMissed": top_gap["timesMissed"],
                    }
                    if top_gap
                    else None
                ),
            }
        )
    return rounds


def _checked(turn: TurnRow) -> bool:
    """The grader recorded what the answer covered: a checklist or a STAR analysis."""
    return bool(turn.rubric_points) or bool(turn.star)


def _missing_points(turn: TurnRow) -> list[dict[str, str]]:
    """Core points the answer did not cover, or stated wrongly: what to add next time.

    A story question has no checklist; its missing STAR parts are listed instead.
    """
    if not turn.rubric_points and turn.star:
        return [
            {"point": name, "status": "MISSING"}
            for key, name in _STAR_PARTS
            if not str(turn.star.get(key) or "").strip()
        ][:MISSING_POINTS + 1]
    out = []
    for point in turn.rubric_points:
        if str(point.get("importance") or "CORE").upper() == "BONUS":
            continue
        status = point.get("status")
        if status in {"MISSING", "PARTIAL", "INCORRECT"} and point.get("description"):
            out.append({"point": str(point["description"])[:200], "status": str(status)})
    return out[:MISSING_POINTS]


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


def build_progress(
    sessions: list[SessionRow],
    turns: list[TurnRow],
    *,
    now: datetime,
    tz_offset_minutes: int = 0,
    weekly_goal: int = DEFAULT_WEEKLY_GOAL,
    all_session_starts: list[datetime] | None = None,
    calibration: Calibration | None = None,
) -> dict[str, Any]:
    """``sessions``: evaluated sessions, oldest first. ``turns``: their graded turns."""
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
            }
        )

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
            skill_questions[key].append(
                {
                    "sessionId": s.session_id,
                    "date": _iso(s.started_at),
                    "question": t.question[:240],
                    "score": round(float(t.score), 1),
                    "missing": _missing_points(t),
                    "checked": _checked(t),
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
            {"sessionId": item["sessionId"], "date": item["date"], "score": item["overallScore"], "tier": item["tier"]}
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
    all_gaps = gap_notebook(sessions, turns_by_session, names, limited=False)
    gaps = {"open": all_gaps["open"][:GAP_OPEN_LIMIT], "fixed": all_gaps["fixed"][:GAP_FIXED_LIMIT]}

    rates = [i["behavior"]["firstTimeSufficientRate"] for i in session_items if i["behavior"]["firstTimeSufficientRate"] is not None]
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
    streak = weekly_streak(
        [_local_day(moment, tz_offset_minutes) for moment in starts],
        today=_local_day(now, tz_offset_minutes),
        goal=weekly_goal,
    )

    resumes: dict[str, dict[str, Any]] = {}
    for item in session_items:
        if item["resumeId"]:
            entry = resumes.setdefault(item["resumeId"], {"resumeId": item["resumeId"], "name": item["resumeName"], "sessions": 0})
            entry["sessions"] += 1
            entry["name"] = item["resumeName"] or entry["name"]

    return {
        "generatedAt": _iso(now),
        "weeklyGoal": weekly_goal,
        "resumes": sorted(resumes.values(), key=lambda r: -r["sessions"]),
        "streak": streak,
        "sessions": session_items,
        "skills": skills,
        "gaps": gaps,
        "readiness": [
            {
                "jobId": job["jobId"],
                "jobTitle": job["jobTitle"],
                "sessions": len(job["points"]),
                "rounds": round_readiness(skills, all_gaps["open"], job["jobId"]),
            }
            for job in jobs
        ],
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
    }


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
            out.append({"kind": "first_challenge", "date": item["date"], "sessionId": item["sessionId"],
                        "jobTitle": item["jobTitle"]})
        rank = _TIER_RANK.get(item["tier"], 1)
        job = item["jobId"] or ""
        if job in best_tier_by_job and rank > best_tier_by_job[job] and item["tier"] != "challenge":
            out.append({"kind": "tier_up", "date": item["date"], "sessionId": item["sessionId"],
                        "jobTitle": item["jobTitle"], "tier": item["tier"]})
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
                out.append({"kind": "skill_level_up", "date": event["date"], "sessionId": event["sessionId"],
                            "skill": skill["label"], "level": event["to"]})
    out.sort(key=lambda m: m["date"])
    return out


def compare_with_previous(progress: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    """"So với lần trước" cho báo cáo cuối buổi.

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
        entry = {"skill": skill["label"], "before": before[-1]["score"], "now": now_point["score"], "delta": round(delta, 1)}
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


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


async def weekly_goal_for(db: AsyncSession, user_id: str) -> int:
    try:
        value = await db.scalar(
            text("SELECT practice_weekly_goal FROM users WHERE id = :uid"), {"uid": user_id}
        )
    except Exception:  # noqa: BLE001 - migration 20261019_0028 not applied yet
        await db.rollback()
        return DEFAULT_WEEKLY_GOAL
    try:
        goal = int(value)
    except (TypeError, ValueError):
        return DEFAULT_WEEKLY_GOAL
    low, high = WEEKLY_GOAL_RANGE
    return goal if low <= goal <= high else DEFAULT_WEEKLY_GOAL


async def set_weekly_goal(db: AsyncSession, user_id: str, goal: int) -> int:
    low, high = WEEKLY_GOAL_RANGE
    if not low <= goal <= high:
        raise ValueError(f"weeklyGoal must be between {low} and {high}")
    await db.execute(
        text("UPDATE users SET practice_weekly_goal = :goal WHERE id = :uid"), {"goal": goal, "uid": user_id}
    )
    await db.commit()
    return goal


async def load_progress(
    db: AsyncSession, user_id: str, *, now: datetime | None = None, tz_offset_minutes: int = 0
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    session_rows = (
        await db.execute(
            text(
                """
                SELECT s.id, s.job_id, j.title AS job_title, s.started_at,
                       s.resume_id, c.filename AS resume_name,
                       p.source_context->>'practiceTier' AS tier,
                       p.plan_payload->'difficulty'->>'level' AS difficulty,
                       ev.id AS evaluation_id, ev.overall_score, ev.grader_version
                FROM interview_sessions s
                JOIN interview_session_plans p ON p.session_id = s.id
                LEFT JOIN job_descriptions j ON j.id = s.job_id
                LEFT JOIN user_cvs c ON c.id = s.resume_id
                LEFT JOIN LATERAL (
                    SELECT e.id, e.overall_score, e.grader_version
                    FROM interview_evaluations e
                    WHERE e.session_id = s.id
                    ORDER BY e.evaluated_at DESC
                    LIMIT 1
                ) ev ON TRUE
                WHERE s.user_id = :uid
                ORDER BY s.started_at DESC
                LIMIT :max_sessions
                """
            ),
            {"uid": user_id, "max_sessions": MAX_SESSIONS},
        )
    ).mappings().all()
    # Every started session counts toward the weekly streak; only graded ones are plotted.
    all_starts = [row["started_at"] for row in session_rows if row["started_at"]]
    evaluated = [row for row in session_rows if row["evaluation_id"] and row["started_at"]]
    sessions = [
        SessionRow(
            session_id=str(row["id"]),
            job_id=str(row["job_id"]) if row["job_id"] else None,
            job_title=row["job_title"],
            started_at=row["started_at"],
            overall_score=float(row["overall_score"]) if row["overall_score"] is not None else None,
            tier=row["tier"] if row["tier"] in _TIER_RANK else "standard",
            difficulty=row["difficulty"],
            resume_id=str(row["resume_id"]) if row["resume_id"] else None,
            resume_name=row["resume_name"],
            grader_version=row.get("grader_version"),
        )
        for row in evaluated
    ]
    turns: list[TurnRow] = []
    if evaluated:
        turn_rows = (
            await db.execute(
                text(
                    """
                    SELECT t.session_id, t.turn_index, t.question_snapshot, te.score, te.outcome,
                           te.rubric_points, te.star_analysis,
                           EXISTS (
                               SELECT 1 FROM interview_chat_messages m
                               WHERE m.turn_id = t.id AND m.message_type = 'PROBE'
                                 AND COALESCE(m.metadata->>'probeKind', 'gap') = 'gap'
                           ) AS needed_probe
                    FROM interview_turn_evaluations te
                    JOIN interview_turns t ON t.id = te.turn_id
                    WHERE te.evaluation_id = ANY(:eval_ids)
                    """
                ),
                {"eval_ids": [row["evaluation_id"] for row in evaluated]},
            )
        ).mappings().all()
        for row in turn_rows:
            snap = _json(row["question_snapshot"]) or {}
            target = snap.get("taxonomyTarget") or {}
            turns.append(
                TurnRow(
                    session_id=str(row["session_id"]),
                    stage=str(snap.get("stage") or ""),
                    concept_id=target.get("conceptId"),
                    label=target.get("label"),
                    difficulty=snap.get("difficulty"),
                    score=float(row["score"]) if row["score"] is not None else None,
                    outcome=str(row["outcome"] or "ANSWERED"),
                    needed_probe=bool(row["needed_probe"]),
                    order=int(row["turn_index"] or 0),
                    rubric_points=[p for p in (_json(row.get("rubric_points")) or []) if isinstance(p, dict)],
                    star=_json(row.get("star_analysis")) or {},
                    question=str(snap.get("questionText") or snap.get("question_text") or ""),
                )
            )
    return build_progress(
        sessions,
        turns,
        now=now,
        tz_offset_minutes=tz_offset_minutes,
        weekly_goal=await weekly_goal_for(db, user_id),
        all_session_starts=all_starts,
        calibration=await load_calibration(db),
    )
