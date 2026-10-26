"""Admin "Chi tiết người dùng": what one account has done in the product.

Account, CVs, interview sessions (with score), matching runs, real-interview
reports and the practice goal, each read with one bounded query. A table that
is missing (older database) yields an empty list instead of failing the page.
Candidate answers are not included; they stay behind the logged
"Xem câu trả lời" action on the session page.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

CV_LIMIT = 20
SESSION_LIMIT = 30
MATCHING_LIMIT = 10
REPORT_LIMIT = 10
ERROR_CHARS = 200


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


async def _rows(db: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in (await db.execute(text(sql), params)).mappings().all()]
    except Exception:  # noqa: BLE001 - table missing on an older database
        await db.rollback()
        return []


def session_mode(raw: Any) -> str:
    mode = str(raw or "text").lower()
    return "video" if mode in {"video", "call"} else "voice" if mode == "voice" else "chat"


def summarize(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [s for s in sessions if s["status"] == "CLOSED"]
    scores = [s["score"] for s in sessions if s["score"] is not None]
    return {
        "sessions": len(sessions),
        "closed": len(closed),
        "graded": len(scores),
        "ungraded": sum(1 for s in closed if s["score"] is None),
        "open": len(sessions) - len(closed),
        "avgScore": round(sum(scores) / len(scores), 2) if scores else None,
        "bestScore": max(scores) if scores else None,
    }


def totals_view(row: dict[str, Any]) -> dict[str, Any]:
    n = int(row.get("n") or 0)
    closed = int(row.get("closed") or 0)
    avg = row.get("avg_score")
    best = row.get("best_score")
    return {
        "sessions": n,
        "closed": closed,
        "graded": int(row.get("graded") or 0),
        "ungraded": int(row.get("ungraded") or 0),
        "open": n - closed,
        "avgScore": round(float(avg), 2) if avg is not None else None,
        "bestScore": round(float(best), 2) if best is not None else None,
    }


async def user_activity(db: AsyncSession, user_id: str) -> dict[str, Any] | None:
    params = {"uid": user_id}
    users = await _rows(
        db,
        "SELECT u.id, u.email, u.name, u.provider, u.is_active, u.created_at, u.practice_weekly_goal, "
        "COALESCE(ARRAY_AGG(ura.role ORDER BY ura.role) FILTER (WHERE ura.role IS NOT NULL), '{}') AS roles "
        "FROM users u LEFT JOIN user_role_assignments ura ON ura.user_id = u.id "
        "WHERE u.id = :uid GROUP BY u.id",
        params,
    )
    if not users:
        return None
    user = users[0]

    cvs = await _rows(
        db,
        "SELECT id, filename, status, error, created_at, updated_at FROM user_cvs "
        "WHERE user_id = :uid ORDER BY created_at DESC LIMIT :limit",
        {**params, "limit": CV_LIMIT},
    )
    sessions = await _rows(
        db,
        """
        SELECT s.id, s.status, LOWER(COALESCE(s.mode, s.type, 'text')) AS mode, s.started_at, s.ended_at,
               s.end_reason, s.duration_minutes, j.title AS job_title,
               (SELECT e.overall_score FROM interview_evaluations e WHERE e.session_id = s.id
                ORDER BY e.evaluated_at DESC LIMIT 1) AS score
        FROM interview_sessions s LEFT JOIN job_descriptions j ON j.id = s.job_id
        WHERE s.user_id = :uid ORDER BY s.started_at DESC NULLS LAST LIMIT :limit
        """,
        {**params, "limit": SESSION_LIMIT},
    )
    # Counts over every session of the user, not only the listed ones.
    session_totals = await _rows(
        db,
        """
        SELECT COUNT(*) AS n,
               COUNT(*) FILTER (WHERE UPPER(COALESCE(s.status, '')) = 'CLOSED') AS closed,
               COUNT(ev.score) AS graded,
               COUNT(*) FILTER (
                   WHERE UPPER(COALESCE(s.status, '')) = 'CLOSED' AND ev.score IS NULL
               ) AS ungraded,
               AVG(ev.score) AS avg_score, MAX(ev.score) AS best_score
        FROM interview_sessions s
        LEFT JOIN LATERAL (
            SELECT e.overall_score AS score FROM interview_evaluations e
            WHERE e.session_id = s.id ORDER BY e.evaluated_at DESC LIMIT 1
        ) ev ON TRUE
        WHERE s.user_id = :uid
        """,
        params,
    )
    matching = await _rows(
        db,
        """
        SELECT m.run_id, MIN(m.created_at) AS started_at,
               (ARRAY_AGG(m.job_id) FILTER (WHERE m.job_id IS NOT NULL))[1] AS job_id,
               BOOL_OR(m.status = 'error') AS has_error,
               BOOL_OR(m.stage = 'done') AS done
        FROM matching_pipeline_events m WHERE m.user_id = :uid
        GROUP BY m.run_id ORDER BY started_at DESC LIMIT :limit
        """,
        {**params, "limit": MATCHING_LIMIT},
    )
    job_ids = sorted({str(r["job_id"]) for r in matching if r.get("job_id")})
    titles = (
        {
            str(r["id"]): r["title"]
            for r in await _rows(
                db, "SELECT id, title FROM job_descriptions WHERE id = ANY(:ids)", {"ids": job_ids}
            )
        }
        if job_ids
        else {}
    )
    reports = await _rows(
        db,
        "SELECT id, company, interview_date, stage, result FROM real_interview_reports "
        "WHERE user_id = :uid ORDER BY interview_date DESC LIMIT :limit",
        {**params, "limit": REPORT_LIMIT},
    )

    session_items = [
        {
            "id": str(s["id"]),
            "status": str(s["status"] or "").upper(),
            "mode": session_mode(s["mode"]),
            "jobTitle": s["job_title"],
            "startedAt": _iso(s["started_at"]),
            "endedAt": _iso(s["ended_at"]),
            "endReason": s["end_reason"],
            "durationMinutes": s["duration_minutes"],
            "score": float(s["score"]) if s["score"] is not None else None,
        }
        for s in sessions
    ]
    return {
        "user": {
            "id": str(user["id"]),
            "email": user["email"],
            "name": user["name"],
            "provider": user["provider"],
            "isActive": bool(user["is_active"]),
            "createdAt": _iso(user["created_at"]),
            "roles": list(user["roles"] or []),
            "weeklyGoal": user.get("practice_weekly_goal"),
        },
        "summary": {
            **(totals_view(session_totals[0]) if session_totals else summarize(session_items)),
            "listed": len(session_items),
            "cvs": len(cvs),
            "cvsFailed": sum(1 for c in cvs if str(c["status"] or "").upper() == "FAILED"),
            "matchingRuns": len(matching),
            "realReports": len(reports),
        },
        "cvs": [
            {
                "id": str(c["id"]),
                "fileName": c["filename"],
                "status": str(c["status"] or "").upper(),
                "error": (c["error"] or "")[:ERROR_CHARS] or None,
                "createdAt": _iso(c["created_at"]),
                "updatedAt": _iso(c["updated_at"]),
            }
            for c in cvs
        ],
        "sessions": session_items,
        "matchingRuns": [
            {
                "runId": str(m["run_id"]),
                "startedAt": _iso(m["started_at"]),
                "jobTitle": titles.get(str(m["job_id"])) if m.get("job_id") else None,
                "status": "error" if m["has_error"] else "done" if m["done"] else "running",
            }
            for m in matching
        ],
        "realReports": [
            {
                "id": str(r["id"]),
                "company": r["company"],
                "date": _iso(r["interview_date"]),
                "stage": r["stage"],
                "result": r["result"],
            }
            for r in reports
        ],
        "limits": {"cvs": CV_LIMIT, "sessions": SESSION_LIMIT, "matchingRuns": MATCHING_LIMIT},
        "generatedAt": datetime.now(UTC).isoformat(),
    }
