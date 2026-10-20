"""AI health across interviews for the admin overview.

Rates and latencies come from the persisted pipeline events and AI call logs
(src/core/pipeline_events.py) over a time window; "stuck" sessions come from
the durable state, so they are found even when no event was recorded.
Every rate is returned with its numerator and denominator so the page never
shows a percentage of nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# A step that has been "started" longer than this without finishing is stuck.
PLAN_STUCK_MINUTES = 5
INTERVIEW_IDLE_MINUTES = 15


def rate(part: int | None, whole: int | None) -> dict[str, Any]:
    part = int(part or 0)
    whole = int(whole or 0)
    return {"count": part, "total": whole, "rate": round(part / whole, 4) if whole else None}


async def _one(db: AsyncSession, sql: str, params: dict[str, Any]) -> dict[str, Any]:
    row = (await db.execute(text(sql), params)).mappings().first()
    return dict(row) if row else {}


async def _all(db: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in (await db.execute(text(sql), params)).mappings().all()]


def _ms(value: Any) -> int | None:
    return int(round(float(value))) if value is not None else None


async def load_health(db: AsyncSession, *, days: int = 7) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    params = {"since": since}

    turns = await _one(
        db,
        """
        SELECT COUNT(*) AS turns,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95,
               COUNT(*) FILTER (WHERE payload->>'ack_source' = 'fallback') AS ack_fallback,
               COUNT(*) FILTER (WHERE payload->>'ack_source' IS NOT NULL) AS ack_total,
               COUNT(*) FILTER (WHERE payload->>'action' = 'PROBE') AS probes,
               COUNT(*) FILTER (WHERE payload->>'action' = 'PROBE'
                                  AND payload->>'generated_by' = 'fixed_fallback') AS probe_fallback,
               COUNT(*) FILTER (WHERE (payload->>'repeats_recent')::boolean) AS repeats,
               COUNT(DISTINCT session_id) AS sessions
        FROM interview_pipeline_events
        WHERE event = 'turn_spoken' AND created_at >= :since
        """,
        params,
    )
    spoken = await _one(
        db,
        """
        SELECT COALESCE(SUM((payload->>'rejected')::int), 0) AS rejected,
               COALESCE(SUM((payload->>'total')::int), 0) AS total
        FROM interview_pipeline_events
        WHERE event = 'spoken_questions_saved' AND created_at >= :since
        """,
        params,
    )
    steps = await _all(
        db,
        """
        SELECT stage,
               COUNT(*) FILTER (WHERE status = 'completed') AS completed,
               COUNT(*) FILTER (WHERE status = 'error') AS errors,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
                 FILTER (WHERE status = 'completed' AND duration_ms IS NOT NULL) AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
                 FILTER (WHERE status = 'completed' AND duration_ms IS NOT NULL) AS p95
        FROM interview_pipeline_events
        WHERE created_at >= :since AND event IN (
            'plan_completed', 'plan_failed', 'question_selection_completed', 'question_selection_failed',
            'evaluation_pipeline_completed', 'evaluation_pipeline_failed', 'candidate_message_processed',
            'candidate_message_failed', 'text_turn_answered', 'text_turn_answer_failed'
        )
        GROUP BY stage
        """,
        params,
    )
    warnings = await _all(
        db,
        """
        SELECT event, status, COUNT(*) AS count, COUNT(DISTINCT session_id) AS sessions,
               MAX(created_at) AS last_at
        FROM interview_pipeline_events
        WHERE created_at >= :since AND status IN ('warning', 'error')
        GROUP BY event, status
        ORDER BY count DESC
        LIMIT 20
        """,
        params,
    )
    ai = await _one(
        db,
        """
        SELECT COUNT(*) AS calls,
               COUNT(*) FILTER (WHERE status = 'error') AS errors,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens
        FROM ai_call_logs WHERE created_at >= :since
        """,
        params,
    )
    purposes = await _all(
        db,
        """
        SELECT purpose, COUNT(*) AS calls, COUNT(*) FILTER (WHERE status = 'error') AS errors,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95,
               COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS tokens
        FROM ai_call_logs WHERE created_at >= :since
        GROUP BY purpose ORDER BY calls DESC LIMIT 25
        """,
        params,
    )
    daily = await _all(
        db,
        """
        SELECT date_trunc('day', created_at) AS day,
               COUNT(*) FILTER (WHERE event = 'turn_spoken') AS turns,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
                 FILTER (WHERE event = 'turn_spoken') AS turn_p95,
               COUNT(*) FILTER (WHERE status = 'error') AS errors
        FROM interview_pipeline_events WHERE created_at >= :since
        GROUP BY 1 ORDER BY 1
        """,
        params,
    )
    evaluation = await _one(
        db,
        """
        SELECT COUNT(*) FILTER (WHERE status = 'DONE') AS done,
               COUNT(*) FILTER (WHERE status = 'FAILED') AS failed,
               COUNT(*) FILTER (WHERE status = 'PROCESSING') AS processing,
               COUNT(*) FILTER (WHERE attempt_count > 1) AS retried,
               COUNT(*) AS total
        FROM interview_evaluation_jobs WHERE updated_at >= :since
        """,
        params,
    )
    stuck = await _all(
        db,
        f"""
        SELECT s.id, s.status, s.started_at, s.updated_at, u.email AS candidate_email, j.title AS job_title,
               CASE
                 WHEN ej.status = 'PROCESSING' AND ej.lease_until < now() THEN 'evaluation_lease_expired'
                 WHEN ej.status = 'FAILED' AND NOT EXISTS (
                     SELECT 1 FROM interview_evaluations e WHERE e.session_id = s.id) THEN 'evaluation_failed'
                 WHEN p.status = 'DRAFT' AND p.updated_at < now() - interval '{PLAN_STUCK_MINUTES} minutes'
                   THEN 'plan_not_ready'
                 WHEN UPPER(s.status) = 'OPEN' AND s.started_at IS NOT NULL
                   AND s.updated_at < now() - interval '{INTERVIEW_IDLE_MINUTES} minutes'
                   THEN 'interview_idle'
               END AS reason
        FROM interview_sessions s
        LEFT JOIN users u ON u.id = s.user_id
        LEFT JOIN job_descriptions j ON j.id = s.job_id
        LEFT JOIN interview_session_plans p ON p.session_id = s.id
        LEFT JOIN interview_evaluation_jobs ej ON ej.session_id = s.id
        WHERE s.updated_at >= :since
        """,
        params,
    )
    stuck = [row for row in stuck if row.get("reason")][:50]

    return {
        "windowDays": days,
        "since": since.isoformat(),
        "turns": {
            "count": int(turns.get("turns") or 0),
            "sessions": int(turns.get("sessions") or 0),
            "p50Ms": _ms(turns.get("p50")),
            "p95Ms": _ms(turns.get("p95")),
            "ackFallback": rate(turns.get("ack_fallback"), turns.get("ack_total")),
            "probeFallback": rate(turns.get("probe_fallback"), turns.get("probes")),
            "repeatedAck": rate(turns.get("repeats"), turns.get("turns")),
        },
        "spokenRewriteRejected": rate(spoken.get("rejected"), spoken.get("total")),
        "steps": [
            {
                "stage": row["stage"],
                "completed": int(row["completed"] or 0),
                "errors": int(row["errors"] or 0),
                "p50Ms": _ms(row["p50"]),
                "p95Ms": _ms(row["p95"]),
            }
            for row in steps
        ],
        "warnings": [
            {
                "event": row["event"],
                "status": row["status"],
                "count": int(row["count"]),
                "sessions": int(row["sessions"]),
                "lastAt": row["last_at"].isoformat() if row.get("last_at") else None,
            }
            for row in warnings
        ],
        "ai": {
            "calls": int(ai.get("calls") or 0),
            "errors": rate(ai.get("errors"), ai.get("calls")),
            "p50Ms": _ms(ai.get("p50")),
            "p95Ms": _ms(ai.get("p95")),
            "inputTokens": int(ai.get("input_tokens") or 0),
            "outputTokens": int(ai.get("output_tokens") or 0),
            "byPurpose": [
                {
                    "purpose": row["purpose"],
                    "calls": int(row["calls"]),
                    "errors": int(row["errors"]),
                    "p50Ms": _ms(row["p50"]),
                    "p95Ms": _ms(row["p95"]),
                    "tokens": int(row["tokens"] or 0),
                }
                for row in purposes
            ],
        },
        "daily": [
            {
                "day": row["day"].date().isoformat() if row.get("day") else None,
                "turns": int(row["turns"] or 0),
                "turnP95Ms": _ms(row["turn_p95"]),
                "errors": int(row["errors"] or 0),
            }
            for row in daily
        ],
        "evaluation": {
            "done": int(evaluation.get("done") or 0),
            "failed": int(evaluation.get("failed") or 0),
            "processing": int(evaluation.get("processing") or 0),
            "retried": int(evaluation.get("retried") or 0),
            "failureRate": rate(evaluation.get("failed"), evaluation.get("total")),
        },
        "stuck": [
            {
                "sessionId": row["id"],
                "reason": row["reason"],
                "status": row["status"],
                "candidateEmail": row["candidate_email"],
                "jobTitle": row["job_title"],
                "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
            }
            for row in stuck
        ],
        "generatedAt": datetime.now(UTC).isoformat(),
    }
