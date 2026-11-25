"""The agent's boundary after Pha 0 of INTERVIEW_AGENT_DECISION_PLAN_V1.

One turn contract for every modality (``core.contracts.interview``), one compiled
graph shared by every request, and no second decision path beside the core engine.
"""

from __future__ import annotations

import importlib
from collections import Counter
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.modules.interviews.agents import InterviewAgent, InterviewCommandRunner
from src.modules.interviews.graph import graph as agent_graph
from src.modules.interviews.core.contracts.interview import CandidateTurnInput

SRC = Path(__file__).resolve().parents[3] / "src" / "modules" / "interviews"


def test_the_agent_has_no_second_turn_contract() -> None:
    """``InterviewAgentRequest``/``handle_turn`` were a parallel, uncalled path (T2.0.3)."""
    assert not hasattr(InterviewAgent, "handle_turn")
    assert InterviewAgent is InterviewCommandRunner  # legacy export; not the per-turn Interviewer Agent
    assert not (SRC / "agent" / "contracts.py").exists()
    for modality in ("chat", "voice", "video_calls"):
        assert not (SRC / "adapters" / modality / "input").exists()
    hits = [p for p in SRC.rglob("*.py") if "InterviewAgentRequest" in p.read_text(encoding="utf-8")]
    assert hits == []


def test_every_modality_uses_the_core_turn_contract() -> None:
    for modality in ("CHAT", "VOICE", "VIDEO"):
        turn = CandidateTurnInput(session_id="s", turn_index=0, text_content="hello", modality=modality)
        assert turn.modality == modality
        assert turn.text_content == "hello"


@pytest.mark.asyncio
async def test_the_compiled_graph_is_built_once_per_checkpointer(monkeypatch) -> None:
    """T2.0.2: 100 executions, one compile."""
    agent_graph.reset_compiled_graphs()
    builds = Counter()
    original = agent_graph.build_interview_graph

    def counting(*args, **kwargs):
        builds["build"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(agent_graph, "build_interview_graph", counting)
    saver = InMemorySaver()
    monkeypatch.setattr(importlib.import_module("src.modules.interviews.graph.checkpoints"), "_checkpointer", saver)

    async def operation(payload):
        return {"echo": payload}

    operations = {name: operation for name in agent_graph.COMMANDS}
    agent = InterviewCommandRunner()
    for i in range(100):
        result = await agent.execute(
            session_id="s", command="respond", operations=operations,
            event_id=f"m-{i}", payload={"content": str(i)},
        )
        assert result == {"echo": {"content": str(i)}}
    assert builds["build"] == 1
    assert agent_graph.compiled_interview_graph(saver) is agent_graph.compiled_interview_graph(saver)
    agent_graph.reset_compiled_graphs()


@pytest.mark.asyncio
async def test_operations_are_scoped_to_the_call_not_the_graph(monkeypatch) -> None:
    """Two requests with different closures share the compiled graph but never each other's operations."""
    agent_graph.reset_compiled_graphs()
    monkeypatch.setattr(importlib.import_module("src.modules.interviews.graph.checkpoints"), "_checkpointer", None)

    def ops_for(tag):
        async def operation(payload):
            return {"tag": tag, **payload}
        return {name: operation for name in agent_graph.COMMANDS}

    agent = InterviewCommandRunner()
    a = await agent.execute(session_id="s", command="open", operations=ops_for("a"))
    b = await agent.execute(session_id="s", command="open", operations=ops_for("b"))
    assert (a["tag"], b["tag"]) == ("a", "b")
    assert agent_graph.current_operations.get() is None
