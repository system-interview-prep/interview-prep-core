"""Bridge the interview API and the LangGraph agent without duplicating business logic."""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.modules.ai.budget import budget_scope
from src.modules.interviews.agents.planner import PlannerAgent
from src.modules.interviews.graph.command_runner import InterviewCommandRunner
from src.modules.interviews.graph.ports import InterviewOperations
from src.modules.interviews.graph.state import InterviewCommand
from src.modules.interviews.memory import candidate_memory, extraction_job
from src.modules.interviews.session.chat_runtime import (
    complete_chat_session,
    process_candidate_message,
    start_chat_session,
)
from src.modules.interviews.question_bank.suggestions import enqueue_session_question_suggestions
from src.modules.interviews.session.spoken_questions import add_spoken_versions
from src.modules.interviews.agents.evaluator.report import (
    evaluate_closed_session,
    get_session_evaluation,
)


def database_operations(
    *, db: AsyncSession, user: dict, session_row: dict, on_early_speech: Any = None
) -> InterviewOperations:
    """Bind trusted ownership-checked context outside checkpointed state.

    ``on_early_speech`` is a live callable and therefore travels here, in the
    closure, not in ``payload``: payload is LangGraph state and is
    checkpointed to PostgreSQL (V2 brief §8 -- no live objects in
    persistent state). The Hybrid path calls it with the Talker's guarded
    acknowledgment before the graph finishes.
    """
    session_id = str(session_row["id"])
    planner = PlannerAgent()

    async def load_candidate_settings() -> None:
        user_id = str(session_row.get("user_id") or user.get("sub") or "")
        if not user_id:
            return
        result = await db.execute(
            text(
                "SELECT ai_voice, speech_rate, auto_camera_webrtc, ai_tone "
                "FROM candidate_settings WHERE user_id = :user_id"
            ),
            {"user_id": user_id},
        )
        row = result.mappings().one_or_none()
        session_row["candidate_settings"] = (
            {
                "aiVoice": row["ai_voice"],
                "speechRate": float(row["speech_rate"]),
                "autoCameraWebRTC": row["auto_camera_webrtc"],
                "aiTone": row["ai_tone"],
            }
            if row is not None
            else {
                "aiVoice": "vi-north-male",
                "speechRate": 1.0,
                "autoCameraWebRTC": True,
                "aiTone": "standard",
            }
        )

    async def prepare(_: dict) -> dict:
        return await planner.prepare(db=db, user=user, session_row=session_row)

    async def freeze(_: dict) -> dict:
        result = await planner.freeze(db=db, session_row=session_row)
        if result.get("status") == "LOCKED":
            # Interviewer-owned wording capability runs after the deterministic
            # queue has been selected and before the session is opened.
            await add_spoken_versions(db=db, session_id=session_id, locale=session_row.get("locale") or "vi-VN")
            result["turns"] = await planner.read_frozen_turns(db=db, session_id=session_id)
        return result

    async def open_session(_: dict) -> dict:
        await load_candidate_settings()
        result = await start_chat_session(db=db, session_row=session_row)
        if candidate_memory.enabled() != "0":
            # First session of a user: seed their memory from the progress gap notebook.
            await candidate_memory.seed_from_gap_notebook(db, str(session_row.get("user_id") or ""))
        return result

    async def respond(payload: dict) -> dict:
        await load_candidate_settings()
        settings = get_settings()
        # Only final transcripts are passed here; video observations are not
        # included in the competency-scoring telemetry.
        # The turn's hard call budget (ai/budget.py): the decider, its memory
        # step, a fallback speech call, the output guards' regeneration and
        # any transport retry all draw on it; nothing past the cap is sent.
        with budget_scope(
            f"turn:{session_id}:{payload.get('client_message_id') or '-'}",
            limit=settings.interview_turn_call_budget,
            session_id=session_id,
            session_limit=settings.interview_session_call_budget,
        ):
            return await process_candidate_message(
                db=db,
                session_row=session_row,
                client_message_id=payload.get("client_message_id"),
                content=payload["content"],
                telemetry={k: v for k, v in payload.get("telemetry", {}).items() if k != "video_observations"},
                modality=payload.get("modality", "CHAT"),
                duration_seconds=payload.get("duration_seconds", 0.0),
                precomputed_evaluation=payload.get("precomputed_evaluation"),
                precomputed_decision=payload.get("precomputed_decision"),
                preview_key=payload.get("preview_key"),
                on_early_speech=on_early_speech,
            )

    async def finish(payload: dict) -> dict:
        return await complete_chat_session(db=db, session_row=session_row, reason=payload["reason"])

    async def score(_: dict) -> dict:
        existing = await get_session_evaluation(db=db, session_id=session_id)
        if existing:
            result = existing
        else:
            await evaluate_closed_session(db=db, session_id=session_id)
            result = await get_session_evaluation(db=db, session_id=session_id)
            if result is None:
                raise RuntimeError("Evaluation completed without a report")
        # Question Bank proposals are generated only after the session is closed
        # and scored; the worker re-checks both conditions and is idempotent.
        enqueue_session_question_suggestions(session_id)
        if not existing and candidate_memory.enabled() == "readwrite":
            # V2 spec §9: the report is persisted and committed above; extraction
            # is a durable, idempotent background job on the existing Celery/
            # RabbitMQ worker (memory/extraction_job.py). The dispatch is
            # recorded in PostgreSQL *before* send_task, so a crash in between
            # is found by the reconciler; if the broker refuses, keep the
            # pre-§9 inline run so the memory is not silently lost. Neither
            # path can undo a finished grade.
            if await extraction_job.mark_queued(db, session_id) is not None:
                if not extraction_job.enqueue_memory_extraction(session_id):
                    await candidate_memory.extract_after_session(db, session_id, evaluation=result)
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
    """Application entry point; the session workflow runner owns graph execution."""
    return await InterviewCommandRunner().execute(
        session_id=session_id, command=command, operations=operations,
        payload=payload, event_id=event_id,
    )
