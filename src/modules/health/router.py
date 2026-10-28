from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.infrastructure.database import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "interview-prep-fastapi-backend"}


@router.get("/health/interview")
async def interview_readiness(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """Readiness signals needed by the canonical interview runtimes.

    This deliberately reports configuration rather than calling paid external
    providers on every probe. Worker registration remains visible in the
    voice-agent logs/metrics.
    """
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Interview database is unavailable") from exc

    settings = get_settings()
    livekit_configured = all(
        (settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret)
    )
    return {
        "status": "ready",
        "database": "ok",
        "legacyInterviewApiEnabled": settings.legacy_interview_api_enabled,
        "voiceLabEnabled": settings.voice_lab_enabled,
        "livekitConfigured": livekit_configured,
    }
