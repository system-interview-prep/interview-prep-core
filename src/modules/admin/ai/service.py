"""Use cases for AI observability views."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.admin.ai.repository import get_ai_call as load_ai_call
from src.modules.admin.ai.repository import list_ai_calls as load_ai_calls
from src.modules.admin.ai.repository import flagged_turn_links
from src.modules.interviews.facade import ai_call_view, summarize_ai_calls


async def list_ai_calls(
    db: AsyncSession,
    *,
    purpose: str | None,
    status: str | None,
    session_id: str | None,
    days: int,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    rows, total, sample = await load_ai_calls(
        db,
        purpose=purpose,
        status=status,
        session_id=session_id,
        days=days,
        limit=limit,
        offset=offset,
    )
    return {
        "success": True,
        "total": total,
        "summary": summarize_ai_calls(sample),
        "calls": [ai_call_view(row) for row in rows],
    }


async def get_ai_call(db: AsyncSession, call_id: int) -> dict[str, Any] | None:
    row = await load_ai_call(db, call_id)
    return {"success": True, "call": ai_call_view(row)} if row is not None else None


async def enrich_flagged_turns(db: AsyncSession, view: dict[str, Any]) -> dict[str, Any]:
    items = view.get("items") or []
    by_trace = await flagged_turn_links(db, [item["traceId"] for item in items])
    for item in items:
        row = by_trace.get(item["traceId"])
        item["sessionId"] = row["session_id"] if row else None
        item["turnIndex"] = row["turn_index"] if row else None
        item["flags"] = list(row["flags"] or []) if row else []
    return view
