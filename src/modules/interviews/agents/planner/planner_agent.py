"""Public Planner Agent API backed by a compiled LangGraph workflow."""

from __future__ import annotations

from typing import Any

from src.modules.interviews.agents.planner.planner_graph import PlannerCommand, build_planner_graph


class PlannerAgent:
    """Run pre-interview planning commands through the Planner LangGraph."""

    def __init__(self) -> None:
        # No checkpointer: the graph is a request-scoped orchestration layer;
        # PostgreSQL stores durable session plans, contracts, and frozen turns.
        self._graph = build_planner_graph()

    async def _run(self, command: PlannerCommand, **state: Any) -> Any:
        # The session workflow invokes this graph from a sync-durable parent.
        # This request-scoped graph has checkpointing disabled, so inheriting
        # the parent's sync durability makes LangGraph await a checkpoint
        # future that this graph does not create.
        result = await self._graph.ainvoke(
            {"command": command, **state}, durability="async"
        )
        return result.get("result")

    async def prepare(
        self, *, db: Any, user: dict[str, Any], session_row: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._run("prepare", db=db, user=user, session_row=session_row)

    async def freeze(self, *, db: Any, session_row: dict[str, Any]) -> dict[str, Any]:
        return await self._run("freeze", db=db, session_row=session_row)

    async def read_frozen_turns(self, *, db: Any, session_id: str) -> list[dict[str, Any]]:
        return await self._run("read_frozen_turns", db=db, session_id=session_id)
