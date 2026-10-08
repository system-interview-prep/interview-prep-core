"""Bridge the interview API and the LangGraph agent without duplicating business logic."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.interviews.agent.interview_agent import InterviewAgent
from src.modules.interviews.agent.ports import InterviewOperations
from src.modules.interviews.agent.state import InterviewCommand
from src.modules.interviews.application.chat_runtime import (
    complete_chat_session,
    process_candidate_message,
    start_chat_session,
)
from src.modules.interviews.evaluation.evaluation_service import (
    evaluate_closed_session,
    get_session_evaluation,
)
from src.modules.interviews.planning.planner import build_and_persist_session_plan
from src.modules.interviews.planning.question_selector import select_and_freeze_questions


def database_operations(*, db: AsyncSession, user: dict, session_row: dict) -> InterviewOperations:
    """Bind trusted ownership-checked context outside checkpointed state."""
    session_id = str(session_row["id"])

    async def prepare(_: dict) -> dict:
        return await build_and_persist_session_plan(db=db, user=user, session_row=session_row)

    async def freeze(_: dict) -> dict:
        return await select_and_freeze_questions(db=db, session_row=session_row)

    async def open_session(_: dict) -> dict:
        return await start_chat_session(db=db, session_row=session_row)

    async def respond(payload: dict) -> dict:
        # Only final transcripts are passed here; video observations are not
        # included in the competency-scoring telemetry.
        return await process_candidate_message(
            db=db,
            session_row=session_row,
            client_message_id=payload.get("client_message_id"),
            content=payload["content"],
            telemetry={k: v for k, v in payload.get("telemetry", {}).items() if k != "video_observations"},
            modality=payload.get("modality", "CHAT"),
            duration_seconds=payload.get("duration_seconds", 0.0),
        )

    async def finish(payload: dict) -> dict:
        return await complete_chat_session(db=db, session_row=session_row, reason=payload["reason"])

    async def score(_: dict) -> dict:
        existing = await get_session_evaluation(db=db, session_id=session_id)
        if existing:
            return existing
        await evaluate_closed_session(db=db, session_id=session_id)
        result = await get_session_evaluation(db=db, session_id=session_id)
        if result is None:
            raise RuntimeError("Evaluation completed without a report")
        return result

    return {
        "prepare": prepare,
        "freeze": freeze,
        "open": open_session,
        "respond": respond,
        "finish": finish,
        "score": score,
    }


async def run_interview_command(
    *,
    session_id: str,
    command: InterviewCommand,
    operations: InterviewOperations,
    payload: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Application entry point; InterviewAgent owns graph execution."""
    return await InterviewAgent().execute(
        session_id=session_id, command=command, operations=operations,
        payload=payload, event_id=event_id,
    )
