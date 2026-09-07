import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db

router = APIRouter(prefix="/interview/video-calls", tags=["video-calls"])


class StartCall(BaseModel):
    roomId: str = Field(min_length=1)
    sessionId: str | None = None


class CallVoiceRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=12_000)
    language: str = Field(default="English", min_length=1, max_length=64)


async def _call(db: AsyncSession, user_id: str, call_id: str) -> dict:
    result = await db.execute(
        text(
            "SELECT id, session_id, status, started_at, ended_at, metadata "
            "FROM video_calls WHERE id = :id AND user_id = :uid"
        ),
        {"id": call_id, "uid": user_id},
    )
    row = result.mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Video call not found")
    return row


@router.post("/start")
async def start_call(
    payload: StartCall, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    session_id = payload.sessionId
    if session_id:
        result = await db.execute(
            text("SELECT 1 FROM interview_sessions WHERE id = :id AND user_id = :uid"),
            {"id": session_id, "uid": user["sub"]},
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Session not found")
    else:
        session_id = str(uuid4())
        await db.execute(
            text(
                "INSERT INTO interview_sessions (id, user_id, type, language, status) "
                "VALUES (:id, :uid, 'Call', 'English', 'OPEN')"
            ),
            {"id": session_id, "uid": user["sub"]},
        )
    call_id = str(uuid4())
    result = await db.execute(
        text(
            "INSERT INTO video_calls (id, user_id, session_id, status, metadata) "
            "VALUES (:id, :uid, :sid, 'ACTIVE', CAST(:metadata AS jsonb)) RETURNING started_at"
        ),
        {
            "id": call_id,
            "uid": user["sub"],
            "sid": session_id,
            "metadata": json.dumps({"roomId": payload.roomId}),
        },
    )
    await db.commit()
    return {"callId": call_id, "roomId": payload.roomId, "startedAt": result.scalar_one().isoformat()}


@router.post("/{call_id}/end")
async def end_call(
    call_id: str, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    await _call(db, user["sub"], call_id)
    result = await db.execute(
        text("UPDATE video_calls SET status = 'ENDED', ended_at = now() WHERE id = :id RETURNING ended_at"),
        {"id": call_id},
    )
    await db.commit()
    return {"callId": call_id, "endedAt": result.scalar_one().isoformat()}


@router.get("")
async def list_calls(user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    result = await db.execute(
        text(
            "SELECT id, session_id, status, started_at, ended_at, metadata "
            "FROM video_calls WHERE user_id = :uid ORDER BY started_at DESC LIMIT 100"
        ),
        {"uid": user["sub"]},
    )
    return [
        {
            "id": row["id"],
            "sessionId": row["session_id"],
            "status": row["status"],
            "startedAt": row["started_at"].isoformat(),
            "endedAt": row["ended_at"].isoformat() if row["ended_at"] else None,
            "roomId": (row["metadata"] or {}).get("roomId", ""),
        }
        for row in result.mappings().all()
    ]


@router.post("/{call_id}/chat-voice")
async def chat_voice_on_call(
    call_id: str,
    payload: CallVoiceRequest,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    call = await _call(db, user["sub"], call_id)
    import base64

    from src.modules.ai.facade import generate_text, synthesize_speech

    try:
        reply = await generate_text(
            instructions=f"You are a professional video interviewer. Answer only in {payload.language}.",
            input_text=payload.prompt,
        )
        audio, mime_type = await synthesize_speech(reply)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="AI provider unavailable") from exc
    metadata = dict(call["metadata"] or {})
    transcript = list(metadata.get("transcript") or [])
    transcript.extend([{"role": "user", "content": payload.prompt}, {"role": "assistant", "content": reply}])
    metadata["transcript"] = transcript[-100:]
    await db.execute(
        text("UPDATE video_calls SET metadata = CAST(:metadata AS jsonb) WHERE id = :id"),
        {"id": call_id, "metadata": json.dumps(metadata)},
    )
    await db.commit()
    return {"reply": reply, "audioBase64": base64.b64encode(audio).decode(), "mimeType": mime_type}
