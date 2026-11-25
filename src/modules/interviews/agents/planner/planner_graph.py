"""LangGraph workflow for Planner Agent commands.

The graph is intentionally request-scoped and has no checkpointer: SQL remains
the durable source of plan and queue state, while transient DB handles stay out
of persistent graph state.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from src.modules.interviews.agents.planner.contract_snapshot import PlannerContractSnapshot
from src.modules.interviews.agents.planner.p1_proposal import propose_p1_target_order
from src.modules.interviews.agents.planner.p2_selection import propose_question_bank_selection
from src.modules.interviews.agents.planner.planner import build_and_persist_session_plan
from src.modules.interviews.agents.planner.project_evidence import extract_project_evidences
from src.modules.interviews.agents.planner.project_question import draft_project_question
from src.modules.interviews.agents.planner.question_selector import read_frozen_turns, select_and_freeze_questions

PlannerCommand = Literal["prepare", "freeze", "read_frozen_turns"]


class PlannerGraphState(TypedDict, total=False):
    command: PlannerCommand
    db: Any
    user: dict[str, Any]
    session_row: dict[str, Any]
    session_id: str
    result: Any


@lru_cache(maxsize=1)
def build_planner_graph():
    """Build a command graph over Planner capabilities and SQL-backed services."""
    graph = StateGraph(PlannerGraphState)

    async def prepare(state: PlannerGraphState) -> dict[str, Any]:
        result = await build_and_persist_session_plan(
            db=state["db"],
            user=state["user"],
            session_row=state["session_row"],
            target_order_proposer=propose_p1_target_order,
            project_evidence_extractor=extract_project_evidences,
        )
        contract = PlannerContractSnapshot.from_plan_blueprint(result.get("blueprint"))
        result["plannerContract"] = contract.model_dump(mode="json")
        return {"result": result}

    async def freeze(state: PlannerGraphState) -> dict[str, Any]:
        session_row = state["session_row"]
        is_vi = str(session_row.get("locale") or "vi").lower().startswith("vi")
        result = await select_and_freeze_questions(
            db=state["db"],
            session_row=session_row,
            project_question_generator=lambda context: draft_project_question(context, is_vi=is_vi),
            question_selection_proposer=propose_question_bank_selection,
        )
        return {"result": result}

    async def read_frozen(state: PlannerGraphState) -> dict[str, Any]:
        return {
            "result": await read_frozen_turns(
                db=state["db"], session_id=state["session_id"]
            )
        }

    graph.add_node("prepare", prepare)
    graph.add_node("freeze", freeze)
    graph.add_node("read_frozen_turns", read_frozen)
    graph.add_conditional_edges(
        START,
        lambda state: state["command"],
        {"prepare": "prepare", "freeze": "freeze", "read_frozen_turns": "read_frozen_turns"},
    )
    graph.add_edge("prepare", END)
    graph.add_edge("freeze", END)
    graph.add_edge("read_frozen_turns", END)
    # This graph is invoked from the checkpointed session workflow. Without
    # explicitly disabling checkpoint inheritance, LangGraph can reuse the
    # parent's Postgres checkpointer for this nested graph and try to serialize
    # request-scoped state such as SQLAlchemy's AsyncSession.
    return graph.compile(checkpointer=False, name="interview_planner_agent")
