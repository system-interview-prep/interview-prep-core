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

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.admin.system.repository import (
    CV_LIMIT, MATCHING_LIMIT, REPORT_LIMIT, SESSION_LIMIT, load_user_activity,
)

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
    data = await load_user_activity(db, user_id)
    if data is None:
        return None
    user = data["user"]
    cvs = data["cvs"]
    sessions = data["sessions"]
    session_totals = data["session_totals"]
    matching = data["matching"]
    titles = data["titles"]
    reports = data["reports"]

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
