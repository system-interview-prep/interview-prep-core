"""Interview test helpers.

``RECORD_TURN_SNAPSHOT=<file>`` records every ``InterviewCoreEngine.handle_turn``
call made by the tests (input, session state, output) as JSON lines. The agent
work compares the new turn graph against this record (INTERVIEW_AGENT_TEST_PLAN_V1
T2.1.6 / T2.D2.1): the engine's *current* behaviour is the reference, whatever the
tests themselves assert. Time-derived fields are recorded but volatile; the
comparison ignores ``time_remaining_seconds`` and ``started_at``.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

import pytest


def _plain(value: Any) -> Any:
    """JSON-safe copy of inputs and outputs (enums, UUIDs, datetimes, pydantic)."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (UUID, datetime, date)):
        return str(value)
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump())
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


@pytest.fixture(autouse=True)
def _record_turn_snapshot(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """``RECORD_TURN_SNAPSHOT=<file>`` records; ``DUAL_RUN_TURNS=1`` is the T2.1.6 gate.

    With ``DUAL_RUN_TURNS=1`` every ``handle_turn`` also runs the agent's turn
    graph on a deep copy of the same input and state, with the test's own
    LLM double, and the two outputs must agree (``decision`` metadata and the
    volatile fields aside). Opt-in because a recording double (prompt lists,
    call counters) would see every call twice.
    """
    path = os.getenv("RECORD_TURN_SNAPSHOT")
    dual = os.getenv("DUAL_RUN_TURNS") == "1"
    if not path and not dual:
        return
    from src.modules.interviews.core.conversation.turn_engine import InterviewCoreEngine

    original = InterviewCoreEngine.handle_turn

    async def recording(self, turn_input, session_state):  # noqa: ANN001
        before = _plain(copy.deepcopy(session_state))
        graph_output = None
        if dual:
            from src.modules.interviews.agents.interviewer.interview_agent import run_turn

            # A deep copy of the engine carries a copy of the test's LLM double, so
            # its prompt lists and call counters only see the engine's own run.
            graph_output = await run_turn(
                copy.deepcopy(self), copy.deepcopy(turn_input), copy.deepcopy(session_state)
            )
        output = await original(self, turn_input, session_state)
        if dual:
            left = _plain(output)
            right = _plain(graph_output)
            for side in (left, right):
                side.pop("time_remaining_seconds", None)
                side.get("metadata", {}).pop("decision", None)
            assert left == right, f"turn graph disagrees with the engine in {request.node.nodeid}"
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "test": request.node.nodeid,
                    "turn_input": _plain(turn_input),
                    "session_state": before,
                    "output": _plain(output),
                }, ensure_ascii=False) + "\n")
        return output

    monkeypatch.setattr(InterviewCoreEngine, "handle_turn", recording)
