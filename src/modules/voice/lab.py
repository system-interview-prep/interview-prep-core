"""Opt-in audio experiment. No question-bank or interview-runtime mutations."""

from time import perf_counter

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.security import current_user
from src.infrastructure.database import get_db

router = APIRouter(prefix="/voice-lab", tags=["voice-lab"])
MAX_AUDIO_BYTES = 10 * 1024 * 1024
ALLOWED_AUDIO_TYPES = {"audio/webm", "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp4"}


class LabSessionRequest(BaseModel):
    sessionId: str = Field(min_length=1)


class LabSpeechRequest(LabSessionRequest):
    text: str = Field(min_length=1, max_length=4096)


async def _ready(session_id: str, user: dict, db: AsyncSession) -> str:
    settings = get_settings()
    if not settings.voice_lab_enabled:
        raise HTTPException(status_code=404, detail="Voice lab disabled")
    owned = await db.execute(
        text("SELECT 1 FROM interview_sessions WHERE id = :sid AND user_id = :uid"),
        {"sid": session_id, "uid": user["sub"]},
    )
    if owned.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="OpenAI API key is not configured")
    return settings.openai_api_key


async def _post(url: str, api_key: str, **kwargs) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            result = await client.post(
                f"https://api.openai.com/v1/{url}",
                headers={"Authorization": f"Bearer {api_key}"},
                **kwargs,
            )
            result.raise_for_status()
            return result
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="OpenAI audio request failed") from exc


@router.post("/transcribe")
async def transcribe(
    sessionId: str = Form(...),
    audio: UploadFile = File(...),
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    api_key = await _ready(sessionId, user, db)
    if audio.content_type not in ALLOWED_AUDIO_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported audio format")
    data = await audio.read(MAX_AUDIO_BYTES + 1)
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio must be between 1 byte and 10 MiB")
    started = perf_counter()
    result = await _post(
        "audio/transcriptions",
        api_key,
        data={"model": get_settings().voice_lab_transcription_model},
        files={"file": (audio.filename or "recording.webm", data, audio.content_type)},
    )
    return {"text": result.json()["text"], "elapsedMs": round((perf_counter() - started) * 1000)}


@router.post("/speak")
async def speak(
    payload: LabSpeechRequest,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    api_key = await _ready(payload.sessionId, user, db)
    settings = get_settings()
    started = perf_counter()
    result = await _post(
        "audio/speech",
        api_key,
        json={
            "model": settings.voice_lab_tts_model,
            "voice": settings.voice_lab_voice,
            "input": payload.text,
            "response_format": "mp3",
        },
    )
    return Response(
        result.content,
        media_type="audio/mpeg",
        headers={"X-Voice-Lab-Elapsed-Ms": str(round((perf_counter() - started) * 1000))},
    )


@router.post("/realtime-token")
async def realtime_token(
    payload: LabSessionRequest,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    api_key = await _ready(payload.sessionId, user, db)
    settings = get_settings()
    result = await _post(
        "realtime/client_secrets",
        api_key,
        json={
            "session": {
                "type": "realtime",
                "model": settings.voice_lab_realtime_model,
                "audio": {"output": {"voice": settings.voice_lab_voice}},
                "instructions": (
                    "You are testing a Vietnamese and English voice interview. "
                    "Do not select or invent approved question-bank questions. "
                    "Ask only questions explicitly supplied by the application."
                ),
            }
        },
    )
    return result.json()
