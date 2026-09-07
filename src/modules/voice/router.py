import base64
import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db
from src.modules.ai.facade import generate_text, synthesize_speech

router = APIRouter(prefix="/ai", tags=["voice"])


class VoiceChatRequest(BaseModel):
    sessionId: str = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=12_000)
    language: str = Field(default="English", min_length=1, max_length=64)


@router.post("/chat-voice")
async def chat_voice(
    payload: VoiceChatRequest,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    owned = await db.execute(
        text("SELECT 1 FROM interview_sessions WHERE id = :sid AND user_id = :uid"),
        {"sid": payload.sessionId, "uid": user["sub"]},
    )
    if owned.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Session not found")
    await db.execute(
        text(
            "INSERT INTO chat_voice_messages (id, session_id, role, content) "
            "VALUES (:id, :sid, 'user', :content)"
        ),
        {"id": str(uuid4()), "sid": payload.sessionId, "content": payload.prompt.strip()},
    )
    try:
        reply = await generate_text(
            instructions=f"You are a professional interviewer. Answer only in {payload.language}.",
            input_text=payload.prompt,
        )
        audio, mime_type = await synthesize_speech(reply)
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=503, detail="AI provider unavailable") from exc
    await db.execute(
        text(
            "INSERT INTO chat_voice_messages (id, session_id, role, content, metadata) "
            "VALUES (:id, :sid, 'assistant', :content, CAST(:metadata AS jsonb))"
        ),
        {
            "id": str(uuid4()),
            "sid": payload.sessionId,
            "content": reply,
            "metadata": json.dumps({"mimeType": mime_type, "provider": "openai"}),
        },
    )
    await db.commit()
    return {"reply": reply, "audioBase64": base64.b64encode(audio).decode(), "mimeType": mime_type}
