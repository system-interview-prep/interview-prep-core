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


async def _call(db: AsyncSession, user_id: str, call_id: str) -> dict:
    result = await db.execute(text("SELECT id, session_id, status, started_at, ended_at, metadata FROM video_calls WHERE id = :id AND user_id = :uid"), {"id": call_id, "uid": user_id})
    row = result.mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Video call not found")
    return row


@router.post("/start")
async def start_call(payload: StartCall, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    session_id = payload.sessionId
    if session_id:
        result = await db.execute(text("SELECT 1 FROM interview_sessions WHERE id = :id AND user_id = :uid"), {"id": session_id, "uid": user["sub"]})
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Session not found")
    else:
        session_id = str(uuid4())
        await db.execute(text("INSERT INTO interview_sessions (id, user_id, type, language, status) VALUES (:id, :uid, 'Call', 'English', 'Open')"), {"id": session_id, "uid": user["sub"]})
    call_id = str(uuid4())
    result = await db.execute(text("INSERT INTO video_calls (id, user_id, session_id, status, metadata) VALUES (:id, :uid, :sid, 'active', CAST(:metadata AS jsonb)) RETURNING started_at"), {"id": call_id, "uid": user["sub"], "sid": session_id, "metadata": json.dumps({"roomId": payload.roomId})})
    await db.commit()
    return {"callId": call_id, "roomId": payload.roomId, "startedAt": result.scalar_one().isoformat()}


@router.post("/{call_id}/end")
async def end_call(call_id: str, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _call(db, user["sub"], call_id)
    result = await db.execute(text("UPDATE video_calls SET status = 'ended', ended_at = now() WHERE id = :id RETURNING ended_at"), {"id": call_id})
    await db.commit()
    return {"callId": call_id, "endedAt": result.scalar_one().isoformat()}


@router.get("")
async def list_calls(user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    result = await db.execute(text("SELECT id, session_id, status, started_at, ended_at, metadata FROM video_calls WHERE user_id = :uid ORDER BY started_at DESC LIMIT 100"), {"uid": user["sub"]})
    return [{"id": row["id"], "sessionId": row["session_id"], "status": row["status"], "startedAt": row["started_at"].isoformat(), "endedAt": row["ended_at"].isoformat() if row["ended_at"] else None, "roomId": (row["metadata"] or {}).get("roomId", "")} for row in result.mappings().all()]
