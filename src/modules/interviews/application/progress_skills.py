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

import math
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from src.modules.interviews.application.progress_calibration import Calibration
from src.modules.interviews.application.skill_display import area_of, is_tracked
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
            (name, "COVERED" if str(turn.star.get(key) or "").strip() else "MISSING")
            for key, name in _STAR_PARTS
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
        top_gap = next((g for g in gaps if g["area"] == area and g["lastSeen"].get("jobId") == job_id), None)
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
        ][: MISSING_POINTS + 1]
    out = []
    for point in turn.rubric_points:
        if str(point.get("importance") or "CORE").upper() == "BONUS":
            continue
        status = point.get("status")
        if status in {"MISSING", "PARTIAL", "INCORRECT"} and point.get("description"):
            out.append({"point": str(point["description"])[:200], "status": str(status)})
    return out[:MISSING_POINTS]


__all__ = [
    "DEFAULT_WEEKLY_GOAL",
    "WEEKLY_GOAL_RANGE",
    "TREND_MIN_POINTS",
    "TREND_WINDOW",
    "FLAT_DELTA",
    "STREAK_HISTORY_WEEKS",
    "VALID_MIN_ANSWERS",
    "ACTIVITY_WEEKS",
    "MAX_SESSIONS",
    "_TIER_RANK",
    "_TECHNICAL_STAGES",
    "_STAR_STAGES",
    "TREND_PROBABILITY",
    "RECOVERED_COVERAGE",
    "LEVEL_EDGES",
    "LEVEL_MARGIN",
    "PROCESS_SD",
    "_NUMBER",
    "SessionRow",
    "TurnRow",
    "_iso",
    "_round",
    "_phi",
    "reliable_trend",
    "kalman_estimates",
    "_band",
    "skill_levels",
    "_comparable",
    "_adjusted",
    "trend",
    "_local_day",
    "_week_start",
    "weekly_streak",
    "activity_days",
    "skill_key",
    "_core_coverage",
    "RECENT_QUESTIONS",
    "MISSING_POINTS",
    "_STAR_PARTS",
    "GAP_OPEN_LIMIT",
    "GAP_FIXED_LIMIT",
    "REASK_AFTER_DAYS",
    "REASK_AFTER_DAYS_LATER",
    "reask_after_days",
    "_point_states",
    "gap_notebook",
    "ROUND_READY",
    "ROUND_ALMOST",
    "ROUND_MIN_SESSIONS",
    "ALWAYS_ASKED",
    "LEVEL_NAMES",
    "LADDER_BASIC",
    "LADDER_SOLID",
    "LADDER_SOLID_RUN",
    "LADDER_DOWN_RUN",
    "READY_LEVEL_INDEX",
    "round_ladder",
    "level_at",
    "READY_HISTORY_WEEKS",
    "ready_history",
    "round_readiness",
    "_checked",
    "_missing_points",
]
