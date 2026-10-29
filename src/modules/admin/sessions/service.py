"""Interview admin use cases and response mapping."""

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.admin.sessions.repository import ANSWER_PREVIEW_CHARS
from src.modules.admin.sessions.repository import list_sessions as load_sessions


def _initials(name: str | None, email: str) -> str:
    raw = (name or "").strip()
    if raw:
        parts = raw.split()
        return (parts[0][0] + parts[-1][0]).upper() if len(parts) >= 2 else raw[:2].upper()
    return email[:2].upper()


async def list_sessions(
    db: AsyncSession, *, mode: str, search: str | None, report: str, limit: int, offset: int
) -> dict[str, Any]:
    rows, summary = await load_sessions(
        db, mode=mode, search=search, report=report, limit=limit, offset=offset
    )
    sessions = []
    for row in rows:
        candidate_name = row["candidate_name"]
        candidate_email = row["candidate_email"]
        raw_mode = (row["effective_mode"] or "").lower()
        ui_type = "video" if raw_mode in ("call", "video") else "voice" if raw_mode == "voice" else "chat"
        is_closed = (row["status"] or "").upper() == "CLOSED"
        started: datetime | None = row["started_at"]
        score = row["ai_score"]
        sessions.append(
            {
                "id": str(row["id"]),
                "candidateName": candidate_name,
                "candidateEmail": candidate_email,
                "candidateInitials": _initials(candidate_name, candidate_email),
                "position": row["job_title"],
                "teamAndLocation": row["locale"] or row["language"] or "",
                "type": ui_type,
                "aiScore": round(float(score) * 10) if score is not None else None,
                "dateLabel": started.strftime("%d/%m/%Y") if started else "",
                "timeLabel": started.strftime("%H:%M") if started else "",
                "status": ("completed" if score is not None else "action_needed") if is_closed else "scheduled",
                "userId": str(row["user_id"]) if row.get("user_id") else None,
                "endReason": row.get("end_reason"),
                "actionLabel": "admin.interviews.action.viewDetailedReport" if is_closed else "admin.interviews.action.manageBooking",
            }
        )
    avg_score = summary["avg_score"]
    return {
        "success": True,
        "total": int(summary["total"] or 0),
        "stats": {
            "active": int(summary["active"] or 0),
            "completed": int(summary["completed"] or 0),
            "ungraded": int(summary.get("ungraded") or 0),
            "avgScore": round(float(avg_score) * 10) if avg_score is not None else None,
        },
        "sessions": sessions,
    }


def candidate_answers_view(rows: list[dict[str, Any]]) -> dict[str, Any]:
    answers = []
    for row in rows:
        content = str(row["content"] or "")
        answers.append(
            {
                "type": row["message_type"],
                "at": row["created_at"].isoformat() if row["created_at"] else None,
                "text": content[:ANSWER_PREVIEW_CHARS],
                "truncated": len(content) > ANSWER_PREVIEW_CHARS,
                "chars": len(content),
            }
        )
    return {"success": True, "answers": answers, "logged": True}


def access_logs_view(rows: list[dict[str, Any]], total: int) -> dict[str, Any]:
    return {
        "success": True,
        "total": total,
        "logs": [
            {
                "id": row["id"],
                "adminEmail": row["admin_email"],
                "action": row["action"],
                "sessionId": row["session_id"],
                "targetId": row["target_id"],
                "detail": row["detail"] or {},
                "candidate": row["candidate_name"] or row["candidate_email"],
                "at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in rows
        ],
    }
