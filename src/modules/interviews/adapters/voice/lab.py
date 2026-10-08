"""Voice experiments and the authenticated LiveKit adapter for Interview Core."""

from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any

import httpx
import jwt
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.security import bearer_scheme, current_user
from src.infrastructure.database import get_db
from src.modules.interviews.application.facade import (
    ChatRuntimeError,
    start_voice_interview,
    submit_voice_answer,
)

router = APIRouter(prefix="/voice-lab", tags=["voice-lab"])
MAX_AUDIO_BYTES = 10 * 1024 * 1024
ALLOWED_AUDIO_TYPES = {"audio/webm", "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp4"}


class LabSessionRequest(BaseModel):
    sessionId: str = Field(min_length=1)


class LiveKitTokenRequest(LabSessionRequest):
    roomName: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class VoiceRuntimeMessage(BaseModel):
    client_message_id: str = Field(alias="clientMessageId", min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=20000)
    duration_seconds: float = Field(default=0.0, alias="durationSeconds", ge=0, le=3600)
    telemetry: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


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


async def _load_voice_session(db: AsyncSession, session_id: str, user_id: str) -> dict:
    settings = get_settings()
    if not settings.voice_lab_enabled:
        raise HTTPException(status_code=404, detail="Voice lab disabled")
    result = await db.execute(
        text(
            "SELECT s.id, s.user_id, s.resume_id, s.job_id, s.mode, s.status, s.locale, "
            "s.duration_minutes, s.metadata, s.started_at, p.status AS plan_status "
            "FROM interview_sessions s LEFT JOIN interview_session_plans p ON p.session_id = s.id "
            "WHERE s.id = :sid AND s.user_id = :uid"
        ),
        {"sid": session_id, "uid": user_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Interview session not found")
    session = dict(row)
    if session["mode"] != "voice":
        raise HTTPException(status_code=409, detail="Interview session is not configured for voice")
    if session["plan_status"] != "LOCKED":
        raise HTTPException(status_code=409, detail="Lock the interview plan before starting voice")
    if session["status"] != "OPEN":
        raise HTTPException(status_code=409, detail="Interview session is not open")
    return session


async def _voice_runtime_claims(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict[str, str]:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Voice runtime authorization is required")
    settings = get_settings()
    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience="interview-voice",
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid voice runtime authorization") from exc
    if claims.get("scope") != "interview:voice-runtime" or claims.get("tokenType") != "voice-runtime":
        raise HTTPException(status_code=401, detail="Invalid voice runtime authorization")
    if not claims.get("sub") or not claims.get("sid"):
        raise HTTPException(status_code=401, detail="Invalid voice runtime authorization")
    return {"user_id": str(claims["sub"]), "session_id": str(claims["sid"])}


async def _runtime_session(db: AsyncSession, claims: dict[str, str]) -> dict:
    return await _load_voice_session(db, claims["session_id"], claims["user_id"])


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


@router.post("/livekit/runtime/start")
async def start_livekit_runtime(
    claims: dict[str, str] = Depends(_voice_runtime_claims),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await _runtime_session(db, claims)
    try:
        runtime = await start_voice_interview(db=db, session_row=session)
    except ChatRuntimeError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    opening = next(
        (message for message in reversed(runtime.get("messages", [])) if message.get("role") == "assistant"),
        None,
    )
    if not opening:
        raise HTTPException(status_code=409, detail="Interview runtime has no opening question")
    return {"sessionId": session["id"], "openingMessage": opening, "sessionStatus": runtime["sessionStatus"]}


@router.post("/livekit/runtime/message")
async def send_livekit_runtime_message(
    payload: VoiceRuntimeMessage,
    claims: dict[str, str] = Depends(_voice_runtime_claims),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await _runtime_session(db, claims)
    telemetry = {**payload.telemetry, "transport": "livekit", "modality": "VOICE"}
    try:
        return await submit_voice_answer(
            db=db,
            session_row=session,
            client_message_id=payload.client_message_id,
            content=payload.content,
            telemetry=telemetry,
            duration_seconds=payload.duration_seconds,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ChatRuntimeError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/livekit-token")
async def livekit_token(
    payload: LiveKitTokenRequest,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Issue a room token and a session-scoped credential for the voice worker."""
    session = await _load_voice_session(db, payload.sessionId, user["sub"])
    settings = get_settings()
    if not settings.livekit_url or not settings.livekit_api_key or not settings.livekit_api_secret:
        raise HTTPException(status_code=503, detail="LiveKit is not configured")
    try:
        from livekit import api
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="LiveKit dependencies are not installed") from exc

    runtime_expires = datetime.now(UTC) + timedelta(
        minutes=max(15, min(130, int(session.get("duration_minutes") or 25) + 10))
    )
    runtime_token = jwt.encode(
        {
            "sub": str(user["sub"]),
            "sid": str(session["id"]),
            "scope": "interview:voice-runtime",
            "tokenType": "voice-runtime",
            "aud": "interview-voice",
            "iat": datetime.now(UTC),
            "exp": runtime_expires,
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    token = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(f"candidate-{user['sub']}")
        .with_name("Interview candidate")
        .with_ttl(timedelta(minutes=10))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=payload.roomName,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .with_room_config(
            api.RoomConfiguration(
                agents=[
                    api.RoomAgentDispatch(
                        agent_name=settings.livekit_agent_name,
                        metadata=runtime_token,
                    )
                ]
            )
        )
        .to_jwt()
    )
    return {
        "serverUrl": settings.livekit_url,
        "participantToken": token,
        "agentName": settings.livekit_agent_name,
        "expiresInSeconds": 600,
    }
