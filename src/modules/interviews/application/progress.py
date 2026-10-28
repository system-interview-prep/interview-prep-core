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
STREAK_HISTORY_WEEKS = 12
# A session counts toward the streak with this many answered main questions
# (not the greeting or the closing Q&A). An effort floor, not a score floor:
# a score floor would punish practising the hard rounds (research 24/10).
VALID_MIN_ANSWERS = 3
ACTIVITY_WEEKS = 12
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
    session_days: list[date],
    *,
    today: date,
    goal: int,
    progress_days: list[date] | None = None,
) -> dict[str, Any]:
    """Tuần liên tiếp đạt mục tiêu, tính đến tuần này.

    ``session_days``: ngày của các **buổi hợp lệ** (``valid_session``), không
    phải mọi buổi đã bắt đầu: chuỗi đếm mọi buổi thì sinh ra buổi "giữ chuỗi"
    rỗng (nghiên cứu 24/10, PROGRESS_VISIBLE_STREAK_RESEARCH).

    Tuần này chưa đạt thì chưa tính là đứt (tuần chưa hết). Một tuần đã qua không
    đạt được "giữ chuỗi" nếu tháng của tuần đó chưa dùng lượt giữ nào. Hết lượt
    giữ thì vẫn **gỡ lại** được: tuần ngay sau có ít nhất mục tiêu + 1 buổi hợp
    lệ (mỗi tháng một lần). Tuần vừa qua còn đang chờ gỡ thì ``earnBack`` cho
    biết tuần này cần thêm mấy buổi. ``progress`` đánh dấu tuần có ít nhất một ý
    trong Sổ ôn tập được khắc phục; nó không ảnh hưởng chuỗi.

    Các tuần được xét theo thứ tự thời gian (tuần lỡ đầu tiên của tháng dùng
    lượt giữ), nên ``history`` luôn đủ ``STREAK_HISTORY_WEEKS`` tuần gần nhất.
    """
    counts: dict[date, int] = defaultdict(int)
    for day in session_days:
        counts[_week_start(day)] += 1
    progress_weeks = {_week_start(day) for day in progress_days or []}
    current = _week_start(today)
    this_week = counts.get(current, 0)
    last_week = current - timedelta(days=7)

    # Chronological pass over finished weeks: the first missed week of a month
    # uses the month's freeze, a later one can be repaired by the week after.
    freezes_used: set[tuple[int, int]] = set()
    repairs_used: set[tuple[int, int]] = set()
    states: dict[date, str] = {}
    chain = 0
    earn_back: dict[str, Any] | None = None
    week = min(counts) if counts else current
    while week <= last_week:
        month = (week.year, week.month)
        count = counts.get(week, 0)
        following = counts.get(week + timedelta(days=7), 0)
        if count >= goal:
            states[week] = "met"
            chain += 1
        elif month not in freezes_used:
            freezes_used.add(month)
            states[week] = "frozen"
        elif following >= goal + 1 and month not in repairs_used:
            repairs_used.add(month)
            states[week] = "repaired"
            chain += 1
        else:
            states[week] = "missed"
            chain = 0
            if week == last_week and month not in repairs_used:
                earn_back = {"weekStart": week.isoformat(), "remaining": max(1, goal + 1 - this_week)}
        week += timedelta(days=7)
    streak = chain + (1 if this_week >= goal else 0)

    history: list[dict[str, Any]] = []
    for offset in range(STREAK_HISTORY_WEEKS - 1, -1, -1):
        week = current - timedelta(days=7 * offset)
        count = counts.get(week, 0)
        history.append(
            {
                "weekStart": week.isoformat(),
                "count": count,
                "met": count >= goal,
                "frozen": states.get(week) == "frozen",
                "repaired": states.get(week) == "repaired",
                "progress": week in progress_weeks,
                "current": week == current,
            }
        )
    return {
        "weeks": streak,
        "goal": goal,
        "thisWeekCount": this_week,
        "thisWeekRemaining": max(0, goal - this_week),
        "freezeAvailableThisMonth": (today.year, today.month) not in freezes_used,
        "earnBack": earn_back,
        "history": history,
    }


def activity_days(
    started: list[date], valid: list[date], fixed: list[date], *, today: date, weeks: int = ACTIVITY_WEEKS
) -> dict[str, Any]:
    """Days of the last ``weeks`` weeks with practice, for the calendar.

    A day is shaded by valid sessions (not by how many sessions were started)
    and outlined when a missed point was fixed that day (research 24/10).
    """
    start = _week_start(today) - timedelta(days=7 * (weeks - 1))
    days: dict[date, dict[str, int]] = {}
    for key, values in (("started", started), ("valid", valid), ("fixed", fixed)):
        for day in values:
            if start <= day <= today:
                days.setdefault(day, {"started": 0, "valid": 0, "fixed": 0})[key] += 1
    return {
        "from": start.isoformat(),
        "to": today.isoformat(),
        "days": [{"date": day.isoformat(), **counts} for day, counts in sorted(days.items())],
    }


def small_wins(session_items: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """"Ghi nhận nhỏ": concrete things done for the first time, newest first.

    A missed point covered in a *different* question of the same skill, and the
    first answer of a kind (all key points, STAR complete, a measurable
    result, completing after a follow-up). The progress principle (Amabile &
    Kramer): a specific small win motivates more than a score (research 24/10).
    A point covered again in the same question is left out: it can be memory.
    """
    out: list[dict[str, Any]] = [
        {"kind": "fixed", "date": e["date"], "sessionId": e["sessionId"], "point": e["point"],
         "skill": e["skill"], "area": e["area"], "jobId": e.get("jobId")}
        for e in events
        if e["newQuestion"]
    ]
    for index, item in enumerate(session_items):
        for kind in session_firsts(session_items, index):
            out.append({"kind": f"first_{kind}", "date": item["date"], "sessionId": item["sessionId"],
                        "jobId": item["jobId"]})
    out.sort(key=lambda win: win["date"], reverse=True)
    return out[:WINS_LIMIT]


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
# Spaced re-ask of a missed point (research 24/10): days after it was last
# asked, by how many times it has been asked.
REASK_AFTER_DAYS = {1: 7, 2: 14}
REASK_AFTER_DAYS_LATER = 30


def reask_after_days(times_asked: int) -> int:
    return REASK_AFTER_DAYS.get(times_asked, REASK_AFTER_DAYS_LATER)


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
    now: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Key points the candidate keeps leaving out, and the ones they fixed.

    The research (20/10) found the question candidates ask before a real
    interview is "what will they ask and where am I still weak", which a 0-10
    score per keyword does not answer. A point is followed across sessions by
    its text within one skill (the same question, or STAR part, asked again):

    * ``open``: missed or partly covered the last time it was judged. With
      ``now``, ``dueAt``/``due`` say when it is time to practise it again
      (7, 14, then 30 days after it was last asked).
    * ``fixed``: missed at least once before and covered the last time.
    * ``events``: each time a missed point is covered, oldest first, with
      ``newQuestion`` when it was covered in a different question than the one
      it was last missed in. A point covered again in the very same question
      can be memory of that question (retest effect, research 24/10).

    Job keywords that are no longer asked on their own (JSON, XML) are left
    out; so are the greeting and the closing Q&A.
    """
    state: dict[tuple[str, str], dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
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
                question = t.question[:240]
                was_missed = entry.get("status") not in (None, "COVERED")
                if status == "COVERED" and was_missed:
                    events.append(
                        {
                            "date": _iso(s.started_at),
                            "sessionId": s.session_id,
                            "jobId": s.job_id,
                            "skill": entry["skill"],
                            "skillKey": key,
                            "area": entry["area"],
                            "point": entry["point"],
                            "newQuestion": question != entry.get("lastMissedQuestion"),
                        }
                    )
                if status != "COVERED":
                    entry["lastMissedQuestion"] = question
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
    for item in items:
        item.pop("lastMissedQuestion", None)
    still_open = [item for item in items if item["status"] != "COVERED"]
    if now is not None:
        for item in still_open:
            last = datetime.fromisoformat(item["lastSeen"]["date"])
            due_at = last + timedelta(days=reask_after_days(item["timesAsked"]))
            item["dueAt"] = _iso(due_at)
            item["due"] = now >= due_at
    fixed = [item for item in items if item["status"] == "COVERED" and item["timesMissed"] > 0]
    # Most often missed first; among equals, the most recent (stable sorts).
    still_open.sort(key=lambda item: item["lastSeen"]["date"], reverse=True)
    still_open.sort(key=lambda item: -item["timesMissed"])
    fixed.sort(key=lambda item: item["lastSeen"]["date"], reverse=True)
    if not limited:
        return {"open": still_open, "fixed": fixed, "events": events}
    return {"open": still_open[:GAP_OPEN_LIMIT], "fixed": fixed[:GAP_FIXED_LIMIT], "events": events}


# A round is "ready" when the smoothed level is at Vững (LEVEL_EDGES[2]) and
# rests on more than one session: one interview says little (interviewing.io:
# only about a quarter of people perform consistently between interviews).
ROUND_READY = 7.0
ROUND_ALMOST = 5.5
ROUND_MIN_SESSIONS = 2
# Asked in every session (VALIDATE and BEHAVIORAL presets).
ALWAYS_ASKED = ("project", "teamwork")


# Levels of a round (research 24/10, PROGRESS_TRACKING_PATTERNS_RESEARCH:
# Khan Academy's mastery ladder with written rules). Bắt đầu, Cơ bản, Vững,
# Thành thạo. A level moves on whole sessions, never on one answer, and a
# round counts as "ready" from Vững up.
LEVEL_NAMES = ("start", "basic", "solid", "mastered")
LADDER_BASIC = 5.5  # one session at or above this: Cơ bản
LADDER_SOLID = 7.0  # this many sessions in a row at or above it: Vững
LADDER_SOLID_RUN = 2
LADDER_DOWN_RUN = 2  # sessions in a row below the level's floor: one level down
READY_LEVEL_INDEX = 2


def round_ladder(series: list[dict[str, Any]]) -> dict[str, Any]:
    """The level of one round from its session means, oldest first, with the rules.

    * Bắt đầu → Cơ bản: one session with a mean of ``LADDER_BASIC`` or more.
    * Cơ bản → Vững: ``LADDER_SOLID_RUN`` sessions in a row at ``LADDER_SOLID``.
    * Vững → Thành thạo: one session at ``LADDER_SOLID`` at the challenge tier
      (Khan: the top level only through a harder, mixed test).
    * One level down after ``LADDER_DOWN_RUN`` sessions in a row below the
      level's floor (``LADDER_BASIC`` for Cơ bản/Vững, ``LADDER_SOLID`` for
      Thành thạo): one weak session never moves a level.

    ``next`` says what moves the round up, with the progress already made;
    ``events`` are the level changes with their session.
    """
    level = 0
    solid_run = 0
    low_run = 0
    events: list[dict[str, Any]] = []
    for point in series:
        score = float(point["score"])
        before = level
        good = score >= LADDER_SOLID
        if level == 0 and score >= LADDER_BASIC:
            level = 1
        elif level == 2 and good and point.get("tier") == "challenge":
            level = 3
        if level == 1:
            solid_run = solid_run + 1 if good else 0
            if solid_run >= LADDER_SOLID_RUN:
                level, solid_run = 2, 0
        if level == before and level >= 1:
            floor = LADDER_SOLID if level == 3 else LADDER_BASIC
            low_run = low_run + 1 if score < floor else 0
            if low_run >= LADDER_DOWN_RUN:
                level, low_run, solid_run = level - 1, 0, 0
        else:
            low_run = 0
        if level != before:
            events.append(
                {"sessionId": point["sessionId"], "date": point["date"], "from": before, "to": level}
            )
    if level == 0:
        nxt = {"kind": "reach", "score": LADDER_BASIC, "have": 0, "need": 1}
    elif level == 1:
        nxt = {"kind": "run", "score": LADDER_SOLID, "have": solid_run, "need": LADDER_SOLID_RUN}
    elif level == 2:
        nxt = {"kind": "challenge", "score": LADDER_SOLID, "have": 0, "need": 1}
    else:
        nxt = {"kind": "top", "score": LADDER_SOLID, "have": 0, "need": 0}
    return {
        "level": level,
        "levelName": LEVEL_NAMES[level],
        "next": nxt,
        # One more session below the floor would move the round down.
        "atRisk": low_run == LADDER_DOWN_RUN - 1 and level >= 1,
        "levelEvents": events,
    }


def level_at(events: list[dict[str, Any]], moment: str) -> int:
    """A round's level at an ISO date (its last change on or before it)."""
    level = 0
    for event in events:
        if event["date"] <= moment:
            level = event["to"]
    return level


READY_HISTORY_WEEKS = 8


def ready_history(
    rounds: list[dict[str, Any]], *, today: date, tz_offset_minutes: int = 0
) -> list[dict[str, Any]]:
    """Rounds at Vững or above at the end of each of the last weeks (the staircase).

    The count only moves when a round changes level, so it does not follow the
    noise of single sessions (research 24/10).
    """
    out = []
    current = _week_start(today)
    for offset in range(READY_HISTORY_WEEKS - 1, -1, -1):
        week = current - timedelta(days=7 * offset)
        end = datetime.combine(week + timedelta(days=7), datetime.min.time(), tzinfo=UTC) - timedelta(
            minutes=tz_offset_minutes
        )
        moment = _iso(end)
        count = sum(1 for r in rounds if level_at(r.get("levelEvents") or [], moment) >= READY_LEVEL_INDEX)
        out.append({"weekStart": week.isoformat(), "ready": count})
    return out


def round_readiness(
    skills: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    job_id: str,
    calibration: Calibration | None = None,
) -> list[dict[str, Any]]:
    """Readiness for one position, by interview round (interview_rounds.AREAS).

    Per round: the mean smoothed level of its skills as asked for this job,
    the number of sessions behind it, a status (ready / almost / practice /
    thin = one session only / unseen), and the point missed most often there.
    ``first`` is the round's mean score in its first session and ``trend``
    the noise-aware trend of its per-session means (research 24/10: "lần đầu
    → hiện tại" per round, drawn faint when the change is within the noise).
    """
    calibration = calibration or Calibration()
    per_area: dict[str, dict[str, Any]] = {}
    for skill in skills:
        if skill.get("minor"):
            continue
        points = [p for p in skill["points"] if p.get("jobId") == job_id]
        if not points:
            continue
        last = points[-1]
        level = last.get("estimate") if last.get("estimate") is not None else last["score"]
        entry = per_area.setdefault(skill["area"], {"skills": [], "sessions": set(), "by_session": {}})
        entry["skills"].append(
            {"conceptId": skill["conceptId"], "label": skill["label"], "estimate": round(level, 1)}
        )
        entry["sessions"].update(p["sessionId"] for p in points)
        for p in points:
            bucket = entry["by_session"].setdefault(
                p["sessionId"], {"date": p["date"], "scores": [], "tier": p.get("tier") or "standard"}
            )
            bucket["scores"].append(p["score"])
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
                    "first": None,
                    "trend": None,
                    "series": [],
                    **round_ladder([]),
                }
            )
            continue
        round_series = [
            {
                "sessionId": sid,
                "date": bucket["date"],
                "tier": bucket["tier"],
                "score": round(statistics.mean(bucket["scores"]), 1),
            }
            for sid, bucket in sorted(entry["by_session"].items(), key=lambda item: item[1]["date"])
        ]
        series = [item["score"] for item in round_series]
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
                "first": round(series[0], 1),
                "trend": reliable_trend(series, calibration) if len(series) >= 2 else None,
                "series": round_series,
                **round_ladder(round_series),
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


def valid_session(turns: list[TurnRow]) -> bool:
    """A session that counts toward the weekly streak: enough real answers.

    At least ``VALID_MIN_ANSWERS`` main questions answered (the greeting and
    the closing Q&A do not count). Abandoned or empty sessions do not keep a
    streak alive (GitHub's streak counter produced one-contribution days to
    keep a streak; research 24/10).
    """
    answered = [t for t in turns if t.outcome == "ANSWERED" and is_tracked(t.concept_id, t.stage) and t.stage]
    return len(answered) >= VALID_MIN_ANSWERS


RETRY_SESSIONS = 30
WINS_LIMIT = 10


def retries_by_session(
    skill_questions: dict[str, list[dict[str, Any]]], names: dict[str, str], areas: dict[str, str]
) -> dict[str, list[dict[str, Any]]]:
    """"Lần trước – Lần này": a skill asked again after an answer that missed points.

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
                    "now": {k: record[k] for k in ("question", "score", "missing", "covered", "total", "checked")},
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
    """"Tuần này" (Garmin training status: one state, one sentence, one advice).

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
                "valid": valid_session(turns_by_session.get(s.session_id, [])),
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
    all_gaps = gap_notebook(sessions, turns_by_session, names, limited=False, now=now)
    # Points due for practice first, then the most often missed.
    open_sorted = sorted(all_gaps["open"], key=lambda item: not item.get("due"))
    gaps = {"open": open_sorted[:GAP_OPEN_LIMIT], "fixed": all_gaps["fixed"][:GAP_FIXED_LIMIT]}
    retries = retries_by_session(
        skill_questions, names, {key: area_of(skill_concepts.get(key)) for key in skill_questions}
    )
    wins = small_wins(session_items, all_gaps["events"])

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
            entry = resumes.setdefault(item["resumeId"], {"resumeId": item["resumeId"], "name": item["resumeName"], "sessions": 0})
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

    return {
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
