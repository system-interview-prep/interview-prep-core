"""Live interview board and proactive alerts for the admin pages.

``load_live_sessions`` lists what is running now - interviews in progress,
plans being prepared, grading jobs - with the step each one is at, how long
it has been running and whether anything went wrong, so an admin can open the
right interview without knowing its id.

``load_alerts`` returns the problems an admin should act on without having to
look for them: failed or stuck grading, plans that never finished, errors
during live interviews, voice providers that failed over, and fallback rates,
model errors or latencies above their thresholds over the last hour.

Both read the durable tables and the persisted pipeline events; every number
carries its count so the page never shows a rate of nothing.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# An open session with no activity for this long is not "live" any more.
LIVE_WINDOW_MINUTES = 120
# A session that ended this recently stays on the board as "finished".
RECENTLY_ENDED_MINUTES = 15
# Sessions scanned for the board (counts cover all of them); one page is sent.
LIVE_LIMIT = 5000
LIVE_PAGE_DEFAULT = 30
LIVE_PAGE_MAX = 200
BOARD_GROUPS = ("all", "attention", "interviewing", "preparing", "grading", "finished")
# A room state that has lasted this long needs a look (kept in step with the admin page).
PROCESSING_SLOW_MS = 15_000
WAITING_LONG_MS = 90_000
# Ended sessions stay under "Cần chú ý" when they ended like this.
ABNORMAL_END_REASONS = frozenset({"TECHNICAL_FAILURE", "CANDIDATE_LEFT"})

# Alert thresholds (last hour unless stated).
ALERT_WINDOW_MINUTES = 60
RECENT_ERROR_MINUTES = 15
PLAN_STUCK_MINUTES = 5
INTERVIEW_IDLE_MINUTES = 15
FALLBACK_RATE = 0.30
FALLBACK_MIN_TURNS = 10
AI_ERRORS_MIN = 3
TURN_P95_MS = 8000
TURN_P95_MIN_TURNS = 5
FAILED_LOOKBACK_HOURS = 24


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    return str(value) if value else None


async def _all(db: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in (await db.execute(text(sql), params)).mappings().all()]


async def _optional(db: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows from the event tables, or [] before migration 0029."""
    try:
        return await _all(db, sql, params)
    except Exception:  # noqa: BLE001
        await db.rollback()
        return []


# --------------------------------------------------------------------------- board


def board_step(
    row: dict[str, Any],
    turns: dict[str, Any] | None,
    voice: dict[str, Any] | None,
    last_message: dict[str, Any] | None,
    grading: dict[str, Any] | None,
) -> dict[str, Any]:
    """The step one session is at, for the board: ``{step, state?, since?}``.

    step: planning | selecting | not_started | interviewing | grading |
    grading_failed | finished. ``state`` refines "interviewing" with what the
    room is doing (voice_events.VOICE_STATES, or waiting/processing for chat).
    """
    status = str(row.get("status") or "").upper()
    job_status = str(row.get("job_status") or "").upper()
    if job_status == "PROCESSING":
        return {"step": "grading", "since": _iso(row.get("job_updated_at")), **(grading or {})}
    if job_status == "FAILED" and status == "CLOSED":
        return {"step": "grading_failed", "since": _iso(row.get("job_updated_at"))}
    if status == "CLOSED":
        return {"step": "finished", "since": _iso(row.get("ended_at")), "endReason": row.get("end_reason")}
    plan_status = str(row.get("plan_status") or "").upper()
    if plan_status in {"", "DRAFT"}:
        return {"step": "planning", "since": _iso(row.get("plan_updated_at") or row.get("started_at"))}
    if plan_status == "READY":
        return {"step": "selecting", "since": _iso(row.get("plan_updated_at"))}
    if not last_message:
        return {"step": "not_started", "since": _iso(row.get("plan_updated_at"))}
    if voice and str(row.get("mode") or "").lower() in {"voice", "video"}:
        return {
            "step": "interviewing",
            "state": voice.get("state"),
            "kind": voice.get("kind"),
            "since": voice.get("at"),
        }
    role = str(last_message.get("role") or "")
    return {
        "step": "interviewing",
        "state": "processing" if role == "user" else "waiting_answer",
        "since": _iso(last_message.get("created_at")),
    }


def board_group(step: dict[str, Any]) -> str:
    """interviewing | preparing | grading | finished, for the board's filter."""
    name = step.get("step")
    if name == "interviewing":
        return "interviewing"
    if name in {"planning", "selecting", "not_started"}:
        return "preparing"
    if name in {"grading", "grading_failed"}:
        return "grading"
    return "finished"


def _ms_since(value: Any, now: datetime) -> int | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int((now - value).total_seconds() * 1000)


def needs_attention(session: dict[str, Any], now: datetime) -> bool:
    """Errors, failed grading, a candidate who left, or a room state that has lasted too long."""
    step = session.get("step") or {}
    if int(session.get("errors") or 0) > 0 or step.get("step") == "grading_failed":
        return True
    if step.get("step") == "finished":
        return str(step.get("endReason") or "").upper() in ABNORMAL_END_REASONS
    if step.get("step") != "interviewing":
        return False
    state = step.get("state")
    if state == "candidate_away":
        return True
    since = _ms_since(step.get("since"), now)
    if since is None:
        return False
    return (state == "processing" and since > PROCESSING_SLOW_MS) or (
        state == "waiting_answer" and since > WAITING_LONG_MS
    )


_GROUP_ORDER = {"interviewing": 1, "preparing": 2, "grading": 3, "finished": 4}


def board_page(
    sessions: list[dict[str, Any]],
    *,
    group: str = "all",
    q: str | None = None,
    limit: int = LIVE_PAGE_DEFAULT,
    offset: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Counts per group over every session, then one filtered, sorted page.

    "all" is what still needs watching: every session that has not ended, plus
    ended ones that need attention; the others are only under "finished".
    Order: sessions that need attention, then live rooms, preparing, grading,
    finished; oldest start first inside a group (the longest-running on top).
    """
    now = now or datetime.now(UTC)
    keyed = []
    counts = {key: 0 for key in BOARD_GROUPS}
    for session in sessions:
        grp = board_group(session.get("step") or {})
        attention = needs_attention(session, now)
        session = {**session, "group": grp, "attention": attention}
        keyed.append(session)
        counts["all"] += int(grp != "finished" or attention)
        counts[grp] += 1
        counts["attention"] += int(attention)
    needle = (q or "").strip().casefold()
    selected = [
        s
        for s in keyed
        if (
            (s["group"] != "finished" or s["attention"])
            if group == "all"
            else (s["attention"] if group == "attention" else s["group"] == group)
        )
        and (
            not needle
            or any(
                needle in str(s.get(field) or "").casefold()
                for field in ("candidateName", "candidateEmail", "jobTitle")
            )
        )
    ]
    selected.sort(
        key=lambda s: (not s["attention"], _GROUP_ORDER.get(s["group"], 9), str(s.get("startedAt") or ""))
    )
    limit = max(1, min(int(limit or LIVE_PAGE_DEFAULT), LIVE_PAGE_MAX))
    offset = max(0, int(offset or 0))
    return {
        "sessions": selected[offset : offset + limit],
        "total": len(selected),
        "counts": counts,
        "limit": limit,
        "offset": offset,
    }


async def load_live_sessions(
    db: AsyncSession,
    *,
    group: str = "all",
    q: str | None = None,
    limit: int = LIVE_PAGE_DEFAULT,
    offset: int = 0,
) -> dict[str, Any]:
    rows = await _all(
        db,
        f"""
        SELECT s.id, s.status, s.mode, s.started_at, s.updated_at, s.ended_at, s.end_reason,
               s.duration_minutes, u.name AS candidate_name, u.email AS candidate_email,
               j.title AS job_title, p.status AS plan_status, p.updated_at AS plan_updated_at,
               ej.status AS job_status, ej.updated_at AS job_updated_at, ej.attempt_count
        FROM interview_sessions s
        LEFT JOIN users u ON u.id = s.user_id
        LEFT JOIN job_descriptions j ON j.id = s.job_id
        LEFT JOIN interview_session_plans p ON p.session_id = s.id
        LEFT JOIN interview_evaluation_jobs ej ON ej.session_id = s.id
        WHERE (UPPER(s.status) <> 'CLOSED'
               AND COALESCE(s.updated_at, s.started_at) >= now() - interval '{LIVE_WINDOW_MINUTES} minutes')
           OR ej.status = 'PROCESSING'
           OR (UPPER(s.status) = 'CLOSED'
               AND s.ended_at >= now() - interval '{RECENTLY_ENDED_MINUTES} minutes')
        ORDER BY COALESCE(s.updated_at, s.started_at) DESC
        LIMIT {LIVE_LIMIT}
        """,
        {},
    )
    ids = [str(row["id"]) for row in rows]
    if not ids:
        return {
            **board_page([], group=group, q=q, limit=limit, offset=offset),
            "generatedAt": datetime.now(UTC).isoformat(),
        }
    params = {"ids": ids}
    turns = {
        str(r["session_id"]): r
        for r in await _all(
            db,
            """
            SELECT session_id, COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status = 'ANSWERED') AS answered,
                   MIN(turn_index) FILTER (WHERE status = 'ASKED') AS current,
                   COUNT(*) FILTER (
                       WHERE UPPER(COALESCE(question_snapshot->>'stage', '')) = 'WARM_UP'
                   ) AS warmup
            FROM interview_turns WHERE session_id = ANY(CAST(:ids AS VARCHAR[]))
            GROUP BY session_id
            """,
            params,
        )
    }
    last_messages = {
        str(r["session_id"]): r
        for r in await _all(
            db,
            """
            SELECT DISTINCT ON (session_id) session_id, role, created_at
            FROM interview_chat_messages WHERE session_id = ANY(CAST(:ids AS VARCHAR[]))
            ORDER BY session_id, sequence DESC
            """,
            params,
        )
    }
    voice = {
        str(r["session_id"]): {
            "state": (r["payload"] or {}).get("state"),
            "kind": (r["payload"] or {}).get("kind"),
            "at": _iso(r["created_at"]),
        }
        for r in await _optional(
            db,
            """
            SELECT DISTINCT ON (session_id) session_id, payload, created_at
            FROM interview_pipeline_events
            WHERE session_id = ANY(CAST(:ids AS VARCHAR[])) AND event = 'voice_state'
            ORDER BY session_id, created_at DESC, id DESC
            """,
            params,
        )
    }
    problems = {
        str(r["session_id"]): r
        for r in await _optional(
            db,
            """
            SELECT session_id,
                   COUNT(*) FILTER (WHERE status = 'warning') AS warnings,
                   COUNT(*) FILTER (WHERE status = 'error') AS errors,
                   COUNT(*) FILTER (
                       WHERE event = 'turn_spoken' AND payload->>'ack_source' = 'fallback'
                   ) AS ack_fallbacks,
                   MAX(created_at) AS last_event_at
            FROM interview_pipeline_events WHERE session_id = ANY(CAST(:ids AS VARCHAR[]))
            GROUP BY session_id
            """,
            params,
        )
    }
    grading_ids = [str(r["id"]) for r in rows if str(r.get("job_status") or "").upper() == "PROCESSING"]
    grading: dict[str, dict[str, Any]] = {}
    if grading_ids:
        events = await _optional(
            db,
            """
            SELECT session_id, event, payload, created_at FROM interview_pipeline_events
            WHERE session_id = ANY(CAST(:ids AS VARCHAR[]))
              AND event IN ('evaluation_job_claimed', 'turn_graded')
            ORDER BY created_at, id
            """,
            {"ids": grading_ids},
        )
        by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            by_session[str(event["session_id"])].append(event)
        for sid, items in by_session.items():
            claimed = [i for i, e in enumerate(items) if e["event"] == "evaluation_job_claimed"]
            if not claimed:
                continue
            start = claimed[-1]
            graded = sum(1 for e in items[start + 1 :] if e["event"] == "turn_graded")
            total = (items[start]["payload"] or {}).get("turns_total")
            grading[sid] = {"graded": graded, "total": total}

    now = datetime.now(UTC)
    sessions = []
    for row in rows:
        sid = str(row["id"])
        t = turns.get(sid) or {}
        p = problems.get(sid) or {}
        started = row.get("started_at")
        if isinstance(started, datetime) and started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        ended = row.get("ended_at")
        if isinstance(ended, datetime) and ended.tzinfo is None:
            ended = ended.replace(tzinfo=UTC)
        sessions.append(
            {
                "id": sid,
                "candidateName": row.get("candidate_name"),
                "candidateEmail": row.get("candidate_email"),
                "jobTitle": row.get("job_title"),
                "mode": row.get("mode"),
                "status": row.get("status"),
                "startedAt": _iso(started),
                "elapsedMs": int(((ended or now) - started).total_seconds() * 1000)
                if isinstance(started, datetime)
                else None,
                "budgetMinutes": row.get("duration_minutes"),
                "turns": {
                    "total": int(t.get("total") or 0),
                    "answered": int(t.get("answered") or 0),
                    "current": t.get("current"),
                    "warmup": int(t.get("warmup") or 0),
                },
                "step": board_step(row, t, voice.get(sid), last_messages.get(sid), grading.get(sid)),
                "warnings": int(p.get("warnings") or 0),
                "errors": int(p.get("errors") or 0),
                "ackFallbacks": int(p.get("ack_fallbacks") or 0),
                "lastEventAt": _iso(p.get("last_event_at")),
            }
        )
    return {
        **board_page(sessions, group=group, q=q, limit=limit, offset=offset, now=now),
        "generatedAt": now.isoformat(),
    }


# --------------------------------------------------------------------------- alerts


def _alert(
    kind: str,
    severity: str,
    *,
    count: int,
    sessions: list[dict[str, Any]] | None = None,
    last_at: Any = None,
    **values: Any,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "severity": severity,
        "count": count,
        "sessions": [
            {
                "id": str(s.get("id") or s.get("session_id")),
                "candidate": s.get("candidate_name") or s.get("candidate_email"),
                "jobTitle": s.get("job_title"),
            }
            for s in (sessions or [])[:5]
        ],
        "lastAt": _iso(last_at),
        **values,
    }


def build_alerts(
    *,
    grading_failed: list[dict[str, Any]],
    grading_stuck: list[dict[str, Any]],
    plan_stuck: list[dict[str, Any]],
    idle: list[dict[str, Any]],
    live_errors: list[dict[str, Any]],
    degraded: list[dict[str, Any]],
    turn_stats: dict[str, Any],
    ai_stats: dict[str, Any],
) -> list[dict[str, Any]]:
    """Pure: the alert list from the query results (unit tested)."""
    alerts: list[dict[str, Any]] = []

    def latest(rows: list[dict[str, Any]], key: str) -> Any:
        values = [r.get(key) for r in rows if r.get(key) is not None]
        return max(values) if values else None

    if grading_failed:
        alerts.append(
            _alert(
                "grading_failed",
                "error",
                count=len(grading_failed),
                sessions=grading_failed,
                last_at=latest(grading_failed, "updated_at"),
            )
        )
    if grading_stuck:
        alerts.append(
            _alert(
                "grading_stuck",
                "error",
                count=len(grading_stuck),
                sessions=grading_stuck,
                last_at=latest(grading_stuck, "updated_at"),
            )
        )
    if live_errors:
        alerts.append(
            _alert(
                "live_errors",
                "error",
                count=sum(int(r.get("errors") or 0) for r in live_errors),
                sessions=live_errors,
                last_at=latest(live_errors, "last_at"),
                minutes=RECENT_ERROR_MINUTES,
            )
        )
    if plan_stuck:
        alerts.append(
            _alert(
                "plan_stuck",
                "warning",
                count=len(plan_stuck),
                sessions=plan_stuck,
                last_at=latest(plan_stuck, "updated_at"),
                minutes=PLAN_STUCK_MINUTES,
            )
        )
    if degraded:
        alerts.append(
            _alert(
                "voice_degraded",
                "warning",
                count=len(degraded),
                sessions=degraded,
                last_at=latest(degraded, "last_at"),
                minutes=RECENT_ERROR_MINUTES,
            )
        )
    if idle:
        alerts.append(
            _alert(
                "interview_idle",
                "warning",
                count=len(idle),
                sessions=idle,
                last_at=latest(idle, "updated_at"),
                minutes=INTERVIEW_IDLE_MINUTES,
            )
        )

    turns = int(turn_stats.get("turns") or 0)
    ack_total = int(turn_stats.get("ack_total") or 0)
    ack_fallback = int(turn_stats.get("ack_fallback") or 0)
    if ack_total >= FALLBACK_MIN_TURNS and ack_fallback / ack_total >= FALLBACK_RATE:
        alerts.append(
            _alert(
                "ack_fallback_rate",
                "warning",
                count=ack_fallback,
                total=ack_total,
                rate=round(ack_fallback / ack_total, 4),
                threshold=FALLBACK_RATE,
                minutes=ALERT_WINDOW_MINUTES,
                last_at=turn_stats.get("last_at"),
            )
        )
    probes = int(turn_stats.get("probes") or 0)
    probe_fallback = int(turn_stats.get("probe_fallback") or 0)
    if probes >= FALLBACK_MIN_TURNS and probe_fallback / probes >= FALLBACK_RATE:
        alerts.append(
            _alert(
                "probe_fallback_rate",
                "warning",
                count=probe_fallback,
                total=probes,
                rate=round(probe_fallback / probes, 4),
                threshold=FALLBACK_RATE,
                minutes=ALERT_WINDOW_MINUTES,
                last_at=turn_stats.get("last_at"),
            )
        )
    p95 = turn_stats.get("p95")
    if turns >= TURN_P95_MIN_TURNS and p95 is not None and float(p95) > TURN_P95_MS:
        alerts.append(
            _alert(
                "turn_latency",
                "warning",
                count=turns,
                p95Ms=int(round(float(p95))),
                thresholdMs=TURN_P95_MS,
                minutes=ALERT_WINDOW_MINUTES,
                last_at=turn_stats.get("last_at"),
            )
        )
    calls = int(ai_stats.get("calls") or 0)
    errors = int(ai_stats.get("errors") or 0)
    if errors >= AI_ERRORS_MIN:
        alerts.append(
            _alert(
                "ai_errors",
                "error",
                count=errors,
                total=calls,
                rate=round(errors / calls, 4) if calls else None,
                minutes=ALERT_WINDOW_MINUTES,
                last_at=ai_stats.get("last_error_at"),
            )
        )
    order = {"error": 0, "warning": 1}
    alerts.sort(key=lambda a: order.get(a["severity"], 2))
    return alerts


_SESSION_COLUMNS = (
    "s.id, s.updated_at, u.name AS candidate_name, u.email AS candidate_email, j.title AS job_title "
)
_SESSION_JOINS = "LEFT JOIN users u ON u.id = s.user_id LEFT JOIN job_descriptions j ON j.id = s.job_id "


async def load_alerts(db: AsyncSession) -> dict[str, Any]:
    grading_failed = await _all(
        db,
        f"""
        SELECT {_SESSION_COLUMNS.replace("s.updated_at", "ej.updated_at")}
        FROM interview_evaluation_jobs ej JOIN interview_sessions s ON s.id = ej.session_id {_SESSION_JOINS}
        WHERE ej.status = 'FAILED' AND ej.updated_at >= now() - interval '{FAILED_LOOKBACK_HOURS} hours'
          AND NOT EXISTS (SELECT 1 FROM interview_evaluations e WHERE e.session_id = s.id)
        ORDER BY ej.updated_at DESC
        """,
        {},
    )
    grading_stuck = await _all(
        db,
        f"""
        SELECT {_SESSION_COLUMNS.replace("s.updated_at", "ej.updated_at")}
        FROM interview_evaluation_jobs ej JOIN interview_sessions s ON s.id = ej.session_id {_SESSION_JOINS}
        WHERE ej.status = 'PROCESSING' AND ej.lease_until < now()
        ORDER BY ej.updated_at DESC
        """,
        {},
    )
    plan_stuck = await _all(
        db,
        f"""
        SELECT {_SESSION_COLUMNS.replace("s.updated_at", "p.updated_at")}
        FROM interview_session_plans p JOIN interview_sessions s ON s.id = p.session_id {_SESSION_JOINS}
        WHERE p.status = 'DRAFT' AND UPPER(s.status) <> 'CLOSED'
          AND p.updated_at < now() - interval '{PLAN_STUCK_MINUTES} minutes'
          AND p.updated_at >= now() - interval '{FAILED_LOOKBACK_HOURS} hours'
        ORDER BY p.updated_at DESC
        """,
        {},
    )
    idle = await _all(
        db,
        f"""
        SELECT {_SESSION_COLUMNS}
        FROM interview_sessions s {_SESSION_JOINS}
        WHERE UPPER(s.status) = 'OPEN'
          AND s.updated_at < now() - interval '{INTERVIEW_IDLE_MINUTES} minutes'
          AND s.updated_at >= now() - interval '{LIVE_WINDOW_MINUTES} minutes'
          AND EXISTS (SELECT 1 FROM interview_chat_messages m WHERE m.session_id = s.id)
        ORDER BY s.updated_at DESC
        """,
        {},
    )
    live_errors = await _optional(
        db,
        f"""
        SELECT e.session_id AS id, COUNT(*) AS errors, MAX(e.created_at) AS last_at,
               MAX(u.name) AS candidate_name, MAX(u.email) AS candidate_email, MAX(j.title) AS job_title
        FROM interview_pipeline_events e
        JOIN interview_sessions s ON s.id = e.session_id {_SESSION_JOINS}
        WHERE e.status = 'error' AND e.created_at >= now() - interval '{RECENT_ERROR_MINUTES} minutes'
        GROUP BY e.session_id ORDER BY MAX(e.created_at) DESC
        """,
        {},
    )
    degraded = await _optional(
        db,
        f"""
        SELECT d.session_id AS id, d.last_at,
               u.name AS candidate_name, u.email AS candidate_email, j.title AS job_title
        FROM (
            SELECT session_id, MAX(created_at) AS last_at FROM interview_pipeline_events
            WHERE event = 'voice_provider_degraded'
              AND created_at >= now() - interval '{RECENT_ERROR_MINUTES} minutes'
            GROUP BY session_id
        ) d
        JOIN interview_sessions s ON s.id = d.session_id {_SESSION_JOINS}
        WHERE NOT EXISTS (
            SELECT 1 FROM interview_pipeline_events r
            WHERE r.session_id = d.session_id AND r.event = 'voice_provider_recovered'
              AND r.created_at > d.last_at
        )
        ORDER BY d.last_at DESC
        """,
        {},
    )
    turn_rows = await _optional(
        db,
        f"""
        SELECT COUNT(*) AS turns,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95,
               COUNT(*) FILTER (WHERE payload->>'ack_source' = 'fallback') AS ack_fallback,
               COUNT(*) FILTER (WHERE payload->>'ack_source' IS NOT NULL) AS ack_total,
               COUNT(*) FILTER (WHERE payload->>'action' = 'PROBE') AS probes,
               COUNT(*) FILTER (WHERE payload->>'action' = 'PROBE'
                                  AND payload->>'generated_by' = 'fixed_fallback') AS probe_fallback,
               MAX(created_at) AS last_at
        FROM interview_pipeline_events
        WHERE event = 'turn_spoken' AND created_at >= now() - interval '{ALERT_WINDOW_MINUTES} minutes'
        """,
        {},
    )
    ai_rows = await _optional(
        db,
        f"""
        SELECT COUNT(*) AS calls, COUNT(*) FILTER (WHERE status = 'error') AS errors,
               MAX(created_at) FILTER (WHERE status = 'error') AS last_error_at
        FROM ai_call_logs WHERE created_at >= now() - interval '{ALERT_WINDOW_MINUTES} minutes'
        """,
        {},
    )
    alerts = build_alerts(
        grading_failed=grading_failed,
        grading_stuck=grading_stuck,
        plan_stuck=plan_stuck,
        idle=idle,
        live_errors=live_errors,
        degraded=degraded,
        turn_stats=turn_rows[0] if turn_rows else {},
        ai_stats=ai_rows[0] if ai_rows else {},
    )
    return {
        "alerts": alerts,
        "counts": {
            "error": sum(1 for a in alerts if a["severity"] == "error"),
            "warning": sum(1 for a in alerts if a["severity"] == "warning"),
        },
        "generatedAt": datetime.now(UTC).isoformat(),
    }
