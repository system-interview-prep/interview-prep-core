"""Durable, idempotent execution of session commands.

Decisions inside a turn are made by the core engine (``core.conversation``),
reached through the ``respond`` operation the application layer binds. This
class owns what the engine must not know about: the checkpoint thread per
``(session, command, event)``, the replay of a completed candidate turn, and
the single compiled graph shared by every request.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from src.modules.interviews.graph.checkpoints import get_interview_checkpointer
from src.modules.interviews.graph.graph import compiled_interview_graph, current_operations
from src.modules.interviews.graph.ports import InterviewOperations
from src.modules.interviews.graph.state import InterviewCommand


class InterviewCommandRunner:
    """One modality-agnostic workflow runner for Chat, Voice, and Video."""

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
        graph = compiled_interview_graph(checkpointer)
        thread_key = sha256(f"{session_id}\x00{command}\x00{event_id}".encode()).hexdigest()
        config = {"configurable": {"thread_id": f"interview:{thread_key}"}}
        token = current_operations.set(operations)
        try:
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
        finally:
            current_operations.reset(token)
        return state["result"]
