import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db
from src.modules.ai.facade import generate_text

router = APIRouter(prefix="/ai", tags=["chat"])


class ChatRequest(BaseModel):
    sessionId: str = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=12_000)
    language: str = Field(default="English", min_length=1, max_length=64)


async def _ensure_owned(db: AsyncSession, user_id: str, session_id: str) -> None:
    result = await db.execute(
        text("SELECT 1 FROM interview_sessions WHERE id = :sid AND user_id = :uid"),
        {"sid": session_id, "uid": user_id},
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Session not found")


async def _history(db: AsyncSession, session_id: str) -> list[dict]:
    result = await db.execute(
        text("""
            SELECT id, role, content, metadata, created_at, 'text' AS channel
            FROM chat_text_messages WHERE session_id = :sid
            UNION ALL
            SELECT id, role, content, metadata, created_at, 'voice' AS channel
            FROM chat_voice_messages WHERE session_id = :sid
            ORDER BY created_at, id
        """),
        {"sid": session_id},
    )
    return [dict(row) for row in result.mappings().all()]


@router.post("/chat")
async def chat(
    payload: ChatRequest,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _ensure_owned(db, user["sub"], payload.sessionId)
    await db.execute(
        text(
            "INSERT INTO chat_text_messages (id, session_id, role, content) "
            "VALUES (:id, :sid, 'user', :content)"
        ),
        {"id": str(uuid4()), "sid": payload.sessionId, "content": payload.prompt.strip()},
    )
    history = await _history(db, payload.sessionId)
    transcript = "\n".join(f"{item['role']}: {item['content']}" for item in history[-30:])
    try:
        reply = await generate_text(
            instructions=(
                "You are a professional interview coach. Continue the mock interview. "
                f"Answer only in {payload.language}. Ask one focused question at a time."
            ),
            input_text=transcript,
        )
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="AI provider unavailable"
        ) from exc
    await db.execute(
        text(
            "INSERT INTO chat_text_messages (id, session_id, role, content, metadata) "
            "VALUES (:id, :sid, 'assistant', :content, CAST(:metadata AS jsonb))"
        ),
        {
            "id": str(uuid4()),
            "sid": payload.sessionId,
            "content": reply,
            "metadata": json.dumps({"provider": "openai"}),
        },
    )
    await db.commit()
    return {"reply": reply}


@router.get("/history")
async def history(
    session_id: str = Query(alias="sessionId", min_length=1),
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _ensure_owned(db, user["sub"], session_id)
    items = await _history(db, session_id)
    return {"history": items}
