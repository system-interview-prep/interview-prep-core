from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_SECTION_KEYS = ("skills", "employment", "education", "projects", "certifications", "languages", "evidence")


def _array_length(key: str) -> str:
    return (
        f"CASE WHEN jsonb_typeof(c.parsed_data -> '{key}') = 'array' "
        f"THEN jsonb_array_length(c.parsed_data -> '{key}') ELSE 0 END"
    )

_SECTION_COUNTS_SQL = (
    "CASE WHEN c.parsed_data IS NOT NULL THEN jsonb_build_object("
    + ", ".join(f"'{key}', {_array_length(key)}" for key in _SECTION_KEYS)
    + ") END"
)

_CV_COLUMNS = f"""
    c.id AS cv_id, c.user_id, c.filename, c.size, c.content_type, c.status AS db_status,
    c.parse_source, c.created_at, c.updated_at,
    c.parsed_data -> 'parsing' ->> 'status' AS canonical_status,
    c.parsed_data -> 'parsing' ->> 'parserVersion' AS stored_parser_version,
    CASE WHEN jsonb_typeof(c.parsed_data -> 'parsing' -> 'warnings') = 'array'
         THEN jsonb_array_length(c.parsed_data -> 'parsing' -> 'warnings') END AS stored_warnings_count,
    {_SECTION_COUNTS_SQL} AS stored_section_counts,
    u.name AS user_name, u.email AS user_email
"""

_CV_FROM = "FROM user_cvs c LEFT JOIN users u ON u.id = c.user_id"

_STALE_SQL = "c.updated_at < now() - make_interval(mins => 15)"

_STATUS_FILTERS = {
    "running": f"(c.status IN ('PENDING', 'PARSING') AND NOT ({_STALE_SQL}))",
    "failed": f"(c.status = 'FAILED' OR (c.status IN ('PENDING', 'PARSING') AND {_STALE_SQL}))",
    "review": "(c.status = 'DONE' AND c.parsed_data -> 'parsing' ->> 'status' = 'review_required')",
    "completed": (
        "(c.status = 'DONE' AND COALESCE(c.parsed_data -> 'parsing' ->> 'status', '') <> 'review_required')"
    ),
}

_SEARCH_FILTER = (
    "(LOWER(COALESCE(u.name, '')) LIKE :q OR LOWER(COALESCE(u.email, '')) LIKE :q "
    "OR LOWER(COALESCE(c.filename, '')) LIKE :q)"
)

_EVENTS_AGG = """
    SELECT cv_id,
           COUNT(*) FILTER (WHERE event <> 'cv_upload_duplicate') AS event_count,
           MIN(created_at) FILTER (
             WHERE event = 'cv_parse_claimed'
                OR stage IN ('extract', 'source', 'parse', 'quality', 'save', 'done')
           ) AS started_at,
           MAX(created_at) AS last_at,
           (ARRAY_AGG(stage ORDER BY created_at DESC, id DESC)
              FILTER (WHERE event NOT IN ('cv_upload_duplicate', 'not_claimed')))[1] AS current_stage,
           (ARRAY_AGG(event ORDER BY created_at DESC, id DESC)
              FILTER (WHERE event NOT IN ('cv_upload_duplicate', 'not_claimed')))[1] AS last_event,
           (ARRAY_AGG(event ORDER BY created_at DESC, id DESC) FILTER (WHERE stage = 'done'))[1]
             AS done_event,
           (ARRAY_AGG(created_at ORDER BY created_at DESC, id DESC) FILTER (WHERE stage = 'done'))[1]
             AS done_at,
           (ARRAY_AGG(payload ORDER BY created_at DESC, id DESC) FILTER (WHERE stage = 'done'))[1]
             AS done_payload,
           MIN(created_at) FILTER (WHERE event = 'cv_uploaded') AS uploaded_event_at,
           (ARRAY_AGG(payload ORDER BY created_at DESC, id DESC) FILTER (WHERE event = 'quality_gate'))[1]
             AS quality_payload,
           (ARRAY_AGG(payload ->> 'provider' ORDER BY created_at DESC, id DESC)
              FILTER (WHERE event = 'extraction_completed'))[1] AS provider,
           (ARRAY_AGG(payload ->> 'parser_version' ORDER BY created_at DESC, id DESC)
              FILTER (WHERE payload ->> 'parser_version' IS NOT NULL))[1] AS parser_version,
           COUNT(DISTINCT attempt_id) FILTER (WHERE event = 'cv_parse_claimed' OR stage = 'extract')
             AS attempts,
           ARRAY_AGG(DISTINCT event) FILTER (
             WHERE event IN ('provider_fallback_used', 'cv_dispatch_failed', 'retry_scheduled')
           ) AS flag_events,
           COALESCE(BOOL_OR(event = 'cv_enqueued' AND payload ->> 'reparse' = 'true'), false) AS reparsed
    FROM cv_pipeline_events
    WHERE cv_id = ANY(:ids)
    GROUP BY cv_id
"""

async def _mappings(db: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in (await db.execute(text(sql), params)).mappings().all()]

def cv_runs_query(*, status: str, q: str | None) -> tuple[str, str, bool]:
    """(page SQL, count SQL, has search) for the CV list filters."""
    clauses = ["c.created_at >= now() - make_interval(days => :days)"]
    if status in _STATUS_FILTERS:
        clauses.append(_STATUS_FILTERS[status])
    search = bool(q and q.strip())
    if search:
        clauses.append(_SEARCH_FILTER)
    where = "WHERE " + " AND ".join(clauses)
    page = (
        f"SELECT {_CV_COLUMNS} {_CV_FROM} {where} "
        "ORDER BY c.created_at DESC, c.id LIMIT :limit OFFSET :offset"
    )
    count = f"SELECT COUNT(*) AS total {_CV_FROM} {where}"
    return page, count, search

async def _ai_counts(db: AsyncSession, cv_ids: list[str]) -> dict[str, dict[str, int]]:
    rows = await _mappings(
        db,
        "SELECT cv_id, COUNT(*) AS calls, COUNT(*) FILTER (WHERE status = 'error') AS errors "
        "FROM ai_call_logs WHERE cv_id = ANY(:ids) GROUP BY cv_id",
        {"ids": cv_ids},
    )
    return {str(r["cv_id"]): {"calls": r["calls"], "errors": r["errors"]} for r in rows}


async def fetch_cv_list_data(
    db: AsyncSession, *, limit: int, offset: int, status: str, days: int, q: str | None
) -> tuple[list[dict[str, Any]], int, dict[str, dict[str, Any]], dict[str, dict[str, int]]]:
    page_sql, count_sql, search = cv_runs_query(status=status, q=q)
    params: dict[str, Any] = {"days": days}
    if search:
        params["q"] = f"%{(q or '').strip().lower()}%"
    rows = await _mappings(db, page_sql, {**params, "limit": limit, "offset": offset})
    total_rows = await _mappings(db, count_sql, params)
    total = int((total_rows[0]["total"] if total_rows else 0) or 0)
    ids = [str(row["cv_id"]) for row in rows]
    summaries = {str(row["cv_id"]): row for row in await _mappings(db, _EVENTS_AGG, {"ids": ids})}
    ai = await _ai_counts(db, ids)
    return rows, total, summaries, ai


async def fetch_cv_detail_data(
    db: AsyncSession, cv_id: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    rows = await _mappings(
        db, f"SELECT {_CV_COLUMNS}, c.parsed_data -> 'parsing' AS parsing {_CV_FROM} WHERE c.id = :cid",
        {"cid": cv_id},
    )
    if not rows:
        return None, [], []
    events = await _mappings(
        db,
        "SELECT * FROM (SELECT id, cv_id, attempt_id, user_id, stage, event, status, duration_ms, payload, created_at "
        "FROM cv_pipeline_events WHERE cv_id = :cid ORDER BY created_at DESC, id DESC LIMIT 2000) recent "
        "ORDER BY created_at, id",
        {"cid": cv_id},
    )
    calls = await _mappings(
        db,
        "SELECT * FROM (SELECT * FROM ai_call_logs WHERE cv_id = :cid "
        "ORDER BY created_at DESC, id DESC LIMIT 300) recent ORDER BY created_at, id",
        {"cid": cv_id},
    )
    return rows[0], events, calls
