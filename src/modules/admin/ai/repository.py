"""Persistence queries for AI call observability."""

from typing import Any
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

AI_CALL_COLUMNS = (
    "id, session_id, purpose, provider, model, status, duration_ms, input_tokens, output_tokens, "
    "input_chars, output_chars, error_type, error_message, created_at"
)


async def observed_models(db: AsyncSession, *, since: datetime) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            text(
                "SELECT purpose, provider, model, COUNT(*) AS calls, "
                "COUNT(*) FILTER (WHERE status = 'error') AS errors, MAX(created_at) AS last_at "
                "FROM ai_call_logs WHERE created_at >= :since "
                "GROUP BY purpose, provider, model ORDER BY calls DESC LIMIT 60"
            ),
            {"since": since},
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def list_ai_calls(
    db: AsyncSession,
    *,
    purpose: str | None,
    status: str | None,
    session_id: str | None,
    days: int,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    clauses = ["created_at >= now() - make_interval(days => :days)"]
    params: dict[str, Any] = {"days": days}
    if purpose:
        clauses.append("purpose ILIKE :purpose")
        params["purpose"] = f"%{purpose}%"
    if status:
        clauses.append("status = :status")
        params["status"] = status
    if session_id:
        clauses.append("session_id = :session_id")
        params["session_id"] = session_id
    where = " AND ".join(clauses)
    rows = (
        await db.execute(
            text(
                f"SELECT {AI_CALL_COLUMNS} FROM ai_call_logs WHERE {where} "
                "ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"
            ),
            {**params, "limit": limit, "offset": offset},
        )
    ).mappings().all()
    total = int(
        (await db.execute(text(f"SELECT COUNT(*) FROM ai_call_logs WHERE {where}"), params)).scalar() or 0
    )
    sample = (
        await db.execute(
            text(
                "SELECT purpose, status, duration_ms, input_tokens, output_tokens FROM ai_call_logs "
                f"WHERE {where} ORDER BY created_at DESC LIMIT 5000"
            ),
            params,
        )
    ).mappings().all()
    return [dict(row) for row in rows], total, [dict(row) for row in sample]


async def get_ai_call(db: AsyncSession, call_id: int) -> dict[str, Any] | None:
    row = (await db.execute(text("SELECT * FROM ai_call_logs WHERE id = :id"), {"id": call_id})).mappings().one_or_none()
    return dict(row) if row else None


async def flagged_turn_links(db: AsyncSession, trace_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not trace_ids:
        return {}
    rows = (
        await db.execute(
            text(
                "SELECT session_id, turn_index, payload->>'langfuse_trace_id' AS trace_id, "
                "payload->'flags' AS flags FROM interview_pipeline_events "
                "WHERE event = 'turn_spoken' AND payload->>'langfuse_trace_id' = ANY(:ids)"
            ),
            {"ids": trace_ids},
        )
    ).mappings().all()
    return {str(row["trace_id"]): dict(row) for row in rows}
