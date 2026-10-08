"""The single, modality-neutral LangGraph interview orchestration graph."""

from typing import Any

from langgraph.graph import END, START, StateGraph

from src.modules.interviews.agent.ports import InterviewOperations
from src.modules.interviews.agent.state import InterviewCommand, InterviewGraphState

COMMANDS: tuple[InterviewCommand, ...] = (
    "prepare", "freeze", "open", "respond", "finish", "score"
)


def build_interview_graph(operations: InterviewOperations, *, checkpointer: Any = None) -> Any:
    """Build one graph whose nodes delegate durable business writes to existing services.

    Operations are closures scoped to a request. They never enter checkpoint state.
    A node may be re-executed after a crash, so each write must be idempotent in
    the business database; the checkpoint is not a distributed transaction.
    """
    graph = StateGraph(InterviewGraphState)

    for command in COMMANDS:
        if command not in operations:
            raise ValueError(f"Missing interview operation: {command}")

        async def execute(state: InterviewGraphState, *, _command: InterviewCommand = command) -> dict:
            result = await operations[_command](state.get("payload", {}))
            return {"result": result, "completed": True}

        graph.add_node(command, execute)
        graph.add_edge(command, END)

    graph.add_conditional_edges(START, lambda state: state["command"], {name: name for name in COMMANDS})
    return graph.compile(checkpointer=checkpointer, name="interview_agent")
