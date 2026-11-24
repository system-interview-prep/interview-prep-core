from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


_STALLED_SQL = "make_interval(mins => 10)"

_STATUS_FILTERS = {
    "completed": "done_event = 'match_completed'",
    "failed": f"(done_event = 'match_failed' OR (done_event IS NULL AND last_at < now() - {_STALLED_SQL}))",
    "running": f"(done_event IS NULL AND last_at >= now() - {_STALLED_SQL})",
}

_RUNS_CTE = """
    WITH runs AS (
      SELECT run_id,
             (ARRAY_AGG(flow ORDER BY created_at DESC, id DESC))[1] AS flow,
             MIN(created_at) AS started_at,
             MAX(created_at) AS last_at,
             (ARRAY_AGG(stage ORDER BY created_at DESC, id DESC))[1] AS current_stage,
             (ARRAY_AGG(event ORDER BY created_at DESC, id DESC) FILTER (WHERE stage = 'done'))[1]
               AS done_event,
             (ARRAY_AGG(created_at ORDER BY created_at DESC, id DESC) FILTER (WHERE stage = 'done'))[1]
               AS done_at,
             (ARRAY_AGG(payload ORDER BY created_at DESC, id DESC) FILTER (WHERE stage = 'done'))[1]
               AS done_payload,
             (ARRAY_AGG(payload -> 'warnings' ORDER BY created_at DESC, id DESC)
                FILTER (WHERE event = 'match_scored'))[1] AS scoring_warnings,
             ARRAY_AGG(event ORDER BY created_at, id) FILTER (WHERE status IN ('warning', 'error'))
               AS problem_events,
             (ARRAY_AGG(user_id ORDER BY created_at, id) FILTER (WHERE user_id IS NOT NULL))[1] AS user_id,
             (ARRAY_AGG(resume_id ORDER BY created_at, id) FILTER (WHERE resume_id IS NOT NULL))[1]
               AS resume_id,
             (ARRAY_AGG(job_id ORDER BY created_at, id) FILTER (WHERE job_id IS NOT NULL))[1] AS job_id
      FROM matching_pipeline_events
      WHERE created_at >= now() - make_interval(days => :days)
      GROUP BY run_id
    )
"""

_SEARCH_JOINS = """
    LEFT JOIN user_cvs c ON c.id = runs.resume_id
    LEFT JOIN users u ON u.id = COALESCE(runs.user_id, c.user_id)
    LEFT JOIN job_descriptions j ON j.id = runs.job_id
"""

_SEARCH_FILTER = (
    "(LOWER(COALESCE(u.name, '')) LIKE :q OR LOWER(COALESCE(u.email, '')) LIKE :q "
    "OR LOWER(COALESCE(c.filename, '')) LIKE :q OR LOWER(COALESCE(j.title, '')) LIKE :q)"
)

async def _mappings(db: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in (await db.execute(text(sql), params)).mappings().all()]

async def resolve_names(
    db: AsyncSession, summaries: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Users, CVs and jobs of the runs: one batched query each, deleted rows tolerated."""
    resume_ids = sorted({s["resume_id"] for s in summaries if s.get("resume_id")})
    job_ids = sorted({s["job_id"] for s in summaries if s.get("job_id")})
    cvs: dict[str, dict[str, Any]] = {}
    if resume_ids:
        for row in await _mappings(
            db, "SELECT id, user_id, filename FROM user_cvs WHERE id = ANY(:ids)", {"ids": resume_ids}
        ):
            cvs[str(row["id"])] = {
                "id": str(row["id"]),
                "fileName": row["filename"],
                "userId": row["user_id"],
            }
    user_ids = sorted(
        {s["user_id"] for s in summaries if s.get("user_id")}
        | {cv["userId"] for cv in cvs.values() if cv.get("userId")}
    )
    users: dict[str, dict[str, Any]] = {}
    if user_ids:
        for row in await _mappings(
            db, "SELECT id, name, email FROM users WHERE id = ANY(:ids)", {"ids": user_ids}
        ):
            users[str(row["id"])] = {"id": str(row["id"]), "name": row["name"], "email": row["email"]}
    jobs: dict[str, dict[str, Any]] = {}
    if job_ids:
        for row in await _mappings(
            db, "SELECT id, title FROM job_descriptions WHERE id = ANY(:ids)", {"ids": job_ids}
        ):
            jobs[str(row["id"])] = {"id": str(row["id"]), "title": row["title"]}
    return {"users": users, "cvs": cvs, "jobs": jobs}

async def _ai_counts(db: AsyncSession, run_ids: list[str]) -> dict[str, dict[str, int]]:
    if not run_ids:
        return {}
    rows = await _mappings(
        db,
        "SELECT match_run_id, COUNT(*) AS calls, COUNT(*) FILTER (WHERE status = 'error') AS errors "
        "FROM ai_call_logs WHERE match_run_id = ANY(:ids) GROUP BY match_run_id",
        {"ids": run_ids},
    )
    return {str(r["match_run_id"]): {"calls": r["calls"], "errors": r["errors"]} for r in rows}

def runs_query(*, status: str, q: str | None) -> tuple[str, str, bool]:
    """(page SQL, count SQL, has search) for the run list filters."""
    clauses = []
    if status in _STATUS_FILTERS:
        clauses.append(_STATUS_FILTERS[status])
    search = bool(q and q.strip())
    if search:
        clauses.append(_SEARCH_FILTER)
    joins = _SEARCH_JOINS if search else ""
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    page = (
        f"{_RUNS_CTE} SELECT runs.* FROM runs {joins} {where} "
        "ORDER BY runs.started_at DESC, runs.run_id LIMIT :limit OFFSET :offset"
    )
    count = f"{_RUNS_CTE} SELECT COUNT(*) AS total FROM runs {joins} {where}"
    return page, count, search


async def fetch_run_list_data(
    db: AsyncSession, *, limit: int, offset: int, status: str, days: int, q: str | None
) -> tuple[list[dict[str, Any]], int, dict[str, dict[str, Any]], dict[str, dict[str, int]]]:
    page_sql, count_sql, search = runs_query(status=status, q=q)
    params: dict[str, Any] = {"days": days}
    if search:
        params["q"] = f"%{(q or '').strip().lower()}%"
    rows = await _mappings(db, page_sql, {**params, "limit": limit, "offset": offset})
    total_rows = await _mappings(db, count_sql, params)
    total = int((total_rows[0]["total"] if total_rows else 0) or 0)
    names = await resolve_names(db, rows)
    ai = await _ai_counts(db, [str(row["run_id"]) for row in rows])
    return rows, total, names, ai


async def fetch_run_detail_data(
    db: AsyncSession, run_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    events = await _mappings(
        db,
        "SELECT * FROM (SELECT id, run_id, flow, cache_key, user_id, resume_id, job_id, stage, event, status, "
        "duration_ms, payload, created_at FROM matching_pipeline_events WHERE run_id = :rid "
        "ORDER BY created_at DESC, id DESC LIMIT 2000) recent ORDER BY created_at, id",
        {"rid": run_id},
    )
    if not events:
        return [], [], {"users": {}, "cvs": {}, "jobs": {}}
    calls = await _mappings(
        db, "SELECT * FROM (SELECT * FROM ai_call_logs WHERE match_run_id = :rid "
        "ORDER BY created_at DESC, id DESC LIMIT 300) recent ORDER BY created_at, id", {"rid": run_id}
    )
    identity: dict[str, Any] = {}
    for row in events:
        for key in ("user_id", "resume_id", "job_id"):
            if row.get(key) is not None:
                identity[key] = row[key]
    return events, calls, await resolve_names(db, [identity])
