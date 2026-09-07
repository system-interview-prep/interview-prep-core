import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db
from src.modules.ai.facade import generate_text

router = APIRouter(prefix="/ai", tags=["interview-questions"])


class GenerateQuestionsRequest(BaseModel):
    count: int = Field(default=5, ge=1, le=20)
    position: str = Field(default="General", min_length=1, max_length=255)
    language: str = Field(default="English", min_length=1, max_length=64)


def _parse_questions(raw: str, count: int) -> list[str]:
    try:
        value = json.loads(raw)
        items = value.get("questions", []) if isinstance(value, dict) else value
        questions = [str(item).strip() for item in items if str(item).strip()]
    except (json.JSONDecodeError, TypeError):
        questions = [line.lstrip("-0123456789. ").strip() for line in raw.splitlines()]
        questions = [item for item in questions if item]
    return questions[:count]


@router.post("/session/{session_id}/questions/generate", status_code=status.HTTP_201_CREATED)
async def generate_questions(
    session_id: str,
    payload: GenerateQuestionsRequest,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    owned = await db.execute(
        text("SELECT 1 FROM interview_sessions WHERE id = :sid AND user_id = :uid"),
        {"sid": session_id, "uid": user["sub"]},
    )
    if owned.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        raw = await generate_text(
            instructions=(
                "Create realistic interview questions. Return JSON only in the form "
                '{"questions":["..."]}. Do not include answers.'
            ),
            input_text=(
                f"Position: {payload.position}\nLanguage: {payload.language}\n"
                f"Number of questions: {payload.count}"
            ),
            max_output_tokens=1200,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="AI provider unavailable") from exc
    questions = _parse_questions(raw, payload.count)
    if not questions:
        raise HTTPException(status_code=502, detail="AI provider returned no questions")
    await db.execute(text("DELETE FROM interview_questions WHERE session_id = :sid"), {"sid": session_id})
    for order, question in enumerate(questions, start=1):
        await db.execute(
            text(
                'INSERT INTO interview_questions (id, session_id, "order", question) '
                "VALUES (:id, :sid, :order, :question)"
            ),
            {"id": str(uuid4()), "sid": session_id, "order": order, "question": question},
        )
    plan = {"position": payload.position, "language": payload.language, "count": len(questions)}
    await db.execute(
        text(
            "INSERT INTO interview_question_plans (session_id, plan) VALUES (:sid, CAST(:plan AS jsonb)) "
            "ON CONFLICT (session_id) DO UPDATE SET plan = EXCLUDED.plan, updated_at = now()"
        ),
        {"sid": session_id, "plan": json.dumps(plan)},
    )
    await db.commit()
    return {"sessionId": session_id, "questions": questions, "plan": plan}
