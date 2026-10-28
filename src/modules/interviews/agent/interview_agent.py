"""The one Interview Agent: LangGraph commands plus the existing core kernel."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from src.modules.interviews.agent.checkpoints import get_interview_checkpointer
from src.modules.interviews.agent.contracts import (
    InterviewAgentRequest,
    InterviewAgentResponse,
)
from src.modules.interviews.agent.graph import build_interview_graph
from src.modules.interviews.agent.ports import InterviewOperations
from src.modules.interviews.agent.state import InterviewCommand
from src.modules.interviews.core.interview_engine import InterviewCoreEngine
from src.modules.interviews.core.interview_types import CandidateTurnInput


class InterviewAgent:
    """One modality-agnostic agent for Chat, Voice, and Video turns."""

    def __init__(self, core_engine: InterviewCoreEngine | None = None) -> None:
        self.core_engine = core_engine or InterviewCoreEngine()

    async def execute(
        self,
        *,
        session_id: str,
        command: InterviewCommand,
        operations: InterviewOperations,
        payload: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute one command with a checkpointed, session-scoped graph thread."""
        if command == "respond" and not event_id:
            raise ValueError("Candidate turns require a client_message_id")
        event_id = event_id or command
        normalized_payload = payload or {}
        checkpointer = get_interview_checkpointer()
        graph = build_interview_graph(operations, checkpointer=checkpointer)
        thread_key = sha256(f"{session_id}\x00{command}\x00{event_id}".encode()).hexdigest()
        config = {"configurable": {"thread_id": f"interview:{thread_key}"}}
        if checkpointer is not None:
            previous = await graph.aget_state(config)
            # Only answer turns return a cached result. Other operations must
            # re-check current business state (e.g. P1 cannot run after P2 locks).
            if command == "respond" and previous.values.get("completed"):
                if previous.values.get("payload") != normalized_payload:
                    raise ValueError("EVENT_ID_PAYLOAD_MISMATCH")
                return previous.values["result"]
        invocation = {"session_id": session_id, "command": command, "event_id": event_id,
                      "payload": normalized_payload, "completed": False}
        # `durability` only has meaning when there is something to persist to.
        # Passing it without a checkpointer is rejected by LangGraph, which would
        # turn every checkpointer-less call (tests, scripts, a worker started
        # outside the app lifespan) into an AttributeError deep inside Pregel.
        if checkpointer is None:
            state = await graph.ainvoke(invocation, config=config)
        else:
            state = await graph.ainvoke(invocation, config=config, durability="sync")
        return state["result"]

    async def handle_turn(
        self,
        request: InterviewAgentRequest,
        session_state: Mapping[str, Any],
    ) -> InterviewAgentResponse:
        """Process one final candidate turn through the canonical core engine."""

        core_input = CandidateTurnInput(
            session_id=request.session_id,
            turn_index=request.turn_index,
            text_content=request.text_content,
            modality=request.modality,
            duration_seconds=request.duration_seconds,
            telemetry=request.telemetry,
        )
        output = await self.core_engine.handle_turn(core_input, dict(session_state))
        return InterviewAgentResponse(
            session_id=output.session_id,
            turn_index=output.turn_index,
            action=output.action.value,
            current_stage=output.current_stage.value,
            message_text=output.message_text,
            is_session_finished=output.is_session_finished,
            current_competency=output.current_competency,
            time_remaining_seconds=output.time_remaining_seconds,
            exit_reason=output.exit_reason.value if output.exit_reason else None,
            metadata=output.metadata,
        )
