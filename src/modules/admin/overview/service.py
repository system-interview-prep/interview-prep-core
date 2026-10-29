"""Build admin overview responses from repository data."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.admin.overview import practice_stats
from src.modules.admin.overview.repository import load_overview_metrics


async def get_overview(db: AsyncSession) -> dict:
    metrics = await load_overview_metrics(db)
    sessions = metrics["sessions"]
    avg_duration = sessions.get("avg_duration")
    raw_score = metrics["average_score"]
    return {
        "success": True,
        "overview": {
            "totalCandidates": metrics["total_candidates"],
            "totalSessions": sessions.get("total") or 0,
            "activeSessions": sessions.get("active") or 0,
            "completedSessions": sessions.get("closed") or 0,
            "gradedSessions": sessions.get("graded") or 0,
            "averageScore": round(float(raw_score) * 10, 1) if raw_score is not None else None,
            "averageTurnaround": round(float(avg_duration), 1) if avg_duration else None,
            "sentimentScore": None,
            "monthlyTrend": [
                {"month": row["mon"], "count": row["count"]} for row in metrics["monthly_trend"]
            ],
            "modes": {
                "chat": sessions.get("chat_count") or 0,
                "voice": sessions.get("voice_count") or 0,
                "video": sessions.get("video_count") or 0,
            },
            "cohorts": [],
            "aiEngineStatus": "active",
            "updatedAt": datetime.now(UTC).isoformat(),
        },
    }


async def get_practice(
    db: AsyncSession, *, weeks: int = 8, include_demo: bool = False
) -> dict:
    return {"success": True, **await practice_stats.practice_view(db, weeks=weeks, include_demo=include_demo)}
