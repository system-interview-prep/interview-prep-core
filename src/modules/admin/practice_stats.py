"""Admin "Luyện tập": how candidates practise, week by week.

Counts follow the candidate's progress page (``interviews/application/progress.py``):

* a **valid session** is a graded session with at least ``VALID_MIN_ANSWERS``
  answered main questions (the greeting and the closing Q&A do not count); it is
  what keeps a weekly streak alive;
* a user **meets the weekly goal** when their valid sessions that week reach
  ``users.practice_weekly_goal`` (default 2);
* weeks run Monday to Sunday in Vietnam time.

Demo sessions (3 minutes or less) are left out unless asked for. One query
reads the sessions of the window with their answered-question count; the rest
is counted here so it can be tested without a database.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

VALID_MIN_ANSWERS = 3
DEFAULT_WEEKLY_GOAL = 2
VN_OFFSET = timedelta(hours=7)
DEMO_MAX_MINUTES = 3

_SESSIONS_SQL = """
WITH sess AS (
    SELECT s.id, s.user_id, s.status, s.started_at, s.duration_minutes,
           ev.id AS eval_id, ev.overall_score
    FROM interview_sessions s
    LEFT JOIN LATERAL (
        SELECT e.id, e.overall_score FROM interview_evaluations e
        WHERE e.session_id = s.id ORDER BY e.evaluated_at DESC LIMIT 1
    ) ev ON TRUE
    WHERE s.started_at >= :since {demo_filter}
),
answered AS (
    SELECT te.evaluation_id, COUNT(*) AS n
    FROM interview_turn_evaluations te
    JOIN interview_turns t ON t.id = te.turn_id
    WHERE te.evaluation_id IN (SELECT eval_id FROM sess WHERE eval_id IS NOT NULL)
      AND COALESCE(te.outcome, 'ANSWERED') = 'ANSWERED'
      AND COALESCE(t.question_snapshot->>'stage', '') NOT IN ('', 'WARM_UP', 'CLOSING')
      AND COALESCE(t.question_snapshot->'taxonomyTarget'->>'conceptId', '') <> 'warmup'
    GROUP BY te.evaluation_id
)
SELECT sess.id, sess.user_id, sess.status, sess.started_at, sess.eval_id, sess.overall_score,
       COALESCE(answered.n, 0) AS answered
FROM sess LEFT JOIN answered ON answered.evaluation_id = sess.eval_id
"""


def week_of(moment: datetime) -> date:
    """Monday of the moment's week, in Vietnam time."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    local = (moment.astimezone(UTC) + VN_OFFSET).date()
    return local - timedelta(days=local.weekday())


def summarize_practice(
    rows: list[dict[str, Any]],
    goals: dict[str, int],
    *,
    weeks: int,
    now: datetime,
    reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Weekly series and window totals from session rows (see ``_SESSIONS_SQL``)."""
    current = week_of(now)
    week_keys = [current - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    by_week: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("started_at") is None:
            continue
        week = week_of(row["started_at"])
        if week in week_keys:
            by_week[week].append(row)

    def valid(row: dict[str, Any]) -> bool:
        return bool(row.get("eval_id")) and int(row.get("answered") or 0) >= VALID_MIN_ANSWERS

    series = []
    for week in week_keys:
        items = by_week.get(week, [])
        valid_per_user: Counter[str] = Counter(str(r["user_id"]) for r in items if valid(r))
        users = {str(r["user_id"]) for r in items}
        met = sum(1 for uid, n in valid_per_user.items() if n >= goals.get(uid, DEFAULT_WEEKLY_GOAL))
        scores = [float(r["overall_score"]) for r in items if r.get("overall_score") is not None]
        series.append(
            {
                "week": week.isoformat(),
                "sessions": len(items),
                "graded": sum(1 for r in items if r.get("eval_id")),
                "valid": sum(valid_per_user.values()),
                "activeUsers": len(users),
                "practisingUsers": len(valid_per_user),
                "metGoal": met,
                "avgScore": round(statistics.fmean(scores), 2) if scores else None,
            }
        )

    in_window = [r for week in week_keys for r in by_week.get(week, [])]
    valid_by_user: Counter[str] = Counter(str(r["user_id"]) for r in in_window if valid(r))
    sessions_by_user: Counter[str] = Counter(str(r["user_id"]) for r in in_window)
    closed = [r for r in in_window if str(r.get("status") or "").upper() == "CLOSED"]
    unfinished_graded = [r for r in in_window if r.get("eval_id") and not valid(r)]
    report_results = Counter(str(r.get("result") or "") for r in (reports or []))
    return {
        "weeks": series,
        "totals": {
            "sessions": len(in_window),
            "closed": len(closed),
            "graded": sum(1 for r in in_window if r.get("eval_id")),
            "valid": sum(valid_by_user.values()),
            # Graded but under the answer floor: started, then stopped early.
            "short": len(unfinished_graded),
            "users": len(sessions_by_user),
            "practisingUsers": len(valid_by_user),
            # Practised (valid) in two or more distinct weeks: came back.
            "returningUsers": sum(
                1
                for uid in valid_by_user
                if len({week_of(r["started_at"]) for r in in_window if str(r["user_id"]) == uid and valid(r)})
                >= 2
            ),
            "medianSessionsPerUser": statistics.median(sessions_by_user.values())
            if sessions_by_user
            else None,
        },
        "goalDistribution": dict(
            sorted(Counter(goals.get(uid, DEFAULT_WEEKLY_GOAL) for uid in sessions_by_user).items())
        ),
        "realReports": {
            "total": sum(report_results.values()),
            "passed": report_results.get("passed", 0),
            "failed": report_results.get("failed", 0),
            "waiting": report_results.get("waiting", 0),
        },
        "rules": {"validMinAnswers": VALID_MIN_ANSWERS, "defaultWeeklyGoal": DEFAULT_WEEKLY_GOAL},
    }


async def _rows(db: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in (await db.execute(text(sql), params)).mappings().all()]
    except Exception:  # noqa: BLE001 - table missing on an older database
        await db.rollback()
        return []


async def practice_view(
    db: AsyncSession, *, weeks: int = 8, include_demo: bool = False, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since_week = week_of(now) - timedelta(weeks=weeks - 1)
    # Monday 00:00 Vietnam time, as UTC.
    since = datetime(since_week.year, since_week.month, since_week.day, tzinfo=UTC) - VN_OFFSET
    demo_filter = "" if include_demo else f"AND COALESCE(s.duration_minutes, 25) > {DEMO_MAX_MINUTES}"
    rows = await _rows(db, _SESSIONS_SQL.format(demo_filter=demo_filter), {"since": since})
    user_ids = sorted({str(r["user_id"]) for r in rows if r.get("user_id")})
    goals = (
        {
            str(r["id"]): int(r["goal"] or DEFAULT_WEEKLY_GOAL)
            for r in await _rows(
                db,
                "SELECT id, practice_weekly_goal AS goal FROM users WHERE id = ANY(:ids)",
                {"ids": user_ids},
            )
        }
        if user_ids
        else {}
    )
    reports = await _rows(
        db, "SELECT result FROM real_interview_reports WHERE created_at >= :since", {"since": since}
    )
    view = summarize_practice(rows, goals, weeks=weeks, now=now, reports=reports)
    return {
        **view,
        "windowWeeks": weeks,
        "includeDemo": include_demo,
        "since": since.isoformat(),
        "generatedAt": now.isoformat(),
    }
