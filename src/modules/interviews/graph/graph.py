"""The single, modality-neutral LangGraph interview orchestration graph.

One graph, compiled once per checkpointer, dispatches the six session commands.
The business operations (closures bound to a request's database session and
ownership checks) are supplied per call through ``current_operations`` so the
compiled graph holds no request state and the checkpoint never sees them.
"""

from __future__ import annotations

import contextvars
from typing import Any

from langgraph.graph import END, START, StateGraph

from src.modules.interviews.graph.ports import InterviewOperations
from src.modules.interviews.graph.state import InterviewCommand, InterviewGraphState

COMMANDS: tuple[InterviewCommand, ...] = (
    "prepare", "freeze", "open", "respond", "finish", "score"
)

# Operations of the request being executed. Set by ``InterviewCommandRunner.execute``
# around the graph call; nodes read it instead of closing over it, so the
# compiled graph can be shared. A graph built with explicit ``operations``
# (tests, scripts) falls back to those when nothing is set.
current_operations: contextvars.ContextVar[InterviewOperations | None] = contextvars.ContextVar(
    "interview_operations", default=None
)


def build_interview_graph(
    operations: InterviewOperations | None = None, *, checkpointer: Any = None
) -> Any:
    """Build one graph whose nodes delegate durable business writes to existing services.

    A node may be re-executed after a crash, so each write must be idempotent in
    the business database; the checkpoint is not a distributed transaction.
    """
    if operations is not None:
        missing = [command for command in COMMANDS if command not in operations]
        if missing:
            raise ValueError(f"Missing interview operation: {missing[0]}")
    graph = StateGraph(InterviewGraphState)

    for command in COMMANDS:

        async def execute(state: InterviewGraphState, *, _command: InterviewCommand = command) -> dict:
            active = current_operations.get() or operations
            if active is None or _command not in active:
                raise ValueError(f"Missing interview operation: {_command}")
            result = await active[_command](state.get("payload", {}))
            return {"result": result, "completed": True}

        graph.add_node(command, execute)
        graph.add_edge(command, END)

    graph.add_conditional_edges(START, lambda state: state["command"], {name: name for name in COMMANDS})
    return graph.compile(checkpointer=checkpointer, name="interview_agent")


_compiled: dict[int, Any] = {}


def compiled_interview_graph(checkpointer: Any = None) -> Any:
    """The shared compiled graph for ``checkpointer`` (built on first use)."""
    key = id(checkpointer)
    graph = _compiled.get(key)
    if graph is None:
        graph = build_interview_graph(checkpointer=checkpointer)
        _compiled[key] = graph
    return graph


def reset_compiled_graphs() -> None:
    """Drop the cache (tests swap checkpointers; lifespan ends)."""
    _compiled.clear()
