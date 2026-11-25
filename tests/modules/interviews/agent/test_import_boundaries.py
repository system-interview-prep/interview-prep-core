"""T1.0.4 — import direction of the interview packages.

INTERVIA_INTERVIEW_AGENT_V2_SPEC §10 "Dependency rules":

    api, adapters ─► session, graph ─► agents ─► core, domain services ─► ai.facade

- Agents decide; they must not import LiveKit, STT or TTS, nor reach back up
  into transport, the runtime or the admin/progress read models.
- ``core/`` executes and must not know LangGraph or the agents.
- ``realtime/`` may call the graph runtime, but nothing may call ``realtime``
  from an agent.
- ``prompt/`` only builds strings.

The bounded Planner tools and the checkpointer talk to persistence by design;
those are the listed exemptions.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4] / "src" / "modules" / "interviews"

RULES = {
    # ``progress`` is deliberately absent: §10's chain ends "agents → domain
    # services/contracts", and the Planner reads the gap notebook through
    # ``progress.load_progress`` as a read-only planning hint that fails open.
    # ``admin`` is forbidden because it is the ops read model and notifier --
    # views built *from* what the agents did, never an input to a decision.
    "agents": (
        "sqlalchemy", "psycopg",
        "livekit",
        "src.modules.interviews.adapters",
        "src.modules.interviews.api",
        "src.modules.interviews.admin",
        "src.modules.interviews.session",
    ),
    "core": (
        "langgraph", "langmem", "sqlalchemy",
        "livekit",
        "src.modules.interviews.agents",
        "src.modules.interviews.graph",
        "src.modules.interviews.session",
        "src.modules.interviews.adapters",
    ),
    "core/conversation/prompt": (
        "src.modules.interviews.core.conversation.turn_engine",
        "src.modules.interviews.core.conversation.response_generation",
        "src.modules.interviews.core.conversation.turn_rules",
        "src.modules.ai",
    ),
    # The Main graph owns orchestration and may use persistence, but it must not
    # depend on transport or on a specific modality.
    "graph": (
        "livekit",
        "src.modules.interviews.adapters",
        "src.modules.interviews.api",
    ),
    # Memory is read and written around the agents; it never drives one.
    "memory": (
        "livekit",
        "src.modules.interviews.agents",
        "src.modules.interviews.adapters",
        "src.modules.interviews.api",
    ),
    "blueprint": (
        "livekit",
        "src.modules.interviews.agents",
        "src.modules.interviews.adapters",
        "src.modules.interviews.api",
    ),
}
# Checkpoint storage, the Planner's bounded P1/P2 tools and the Question Bank
# workflows talk to persistence by design. Other modules keep the boundary.
EXEMPT = {
    "graph/checkpoints.py": ("psycopg",),
    # The Planner's bounded P1/P2 tools own their preparation persistence.
    "agents/planner/planner.py": ("sqlalchemy",),
    "agents/planner/question_selector.py": ("sqlalchemy",),
    "agents/planner/entry_gate.py": ("sqlalchemy",),
    "agents/evaluator/evaluation_service.py": ("sqlalchemy",),
    "agents/evaluator/grading_cache.py": ("sqlalchemy",),
    "agents/evaluator/answer_outcome.py": ("sqlalchemy",),
    "memory/candidate_memory.py": ("sqlalchemy",),
    "blueprint/service.py": ("sqlalchemy",),
}


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize("package", sorted(RULES))
def test_package_imports_only_downwards(package: str) -> None:
    forbidden = RULES[package]
    violations = []
    for path in sorted((ROOT / package).rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        allowed_here = EXEMPT.get(rel, ())
        for name in _imports(path):
            if name.startswith(forbidden) and not name.startswith(allowed_here):
                violations.append(f"{rel}: {name}")
    assert violations == [], "\n".join(violations)


def test_every_rule_names_a_package_that_exists() -> None:
    """A renamed package must not silently stop being checked.

    The pre-V2 version of this file still pointed its strictest rule at
    ``agent``; after the move to ``agents`` that rule would have matched no
    files at all and passed vacuously.
    """
    for package in RULES:
        directory = ROOT / package
        assert directory.is_dir(), f"{package} is not a package of {ROOT}"
        assert any(directory.rglob("*.py")), f"{package} has no modules to check"


def test_exemptions_point_at_files_that_exist() -> None:
    """An exemption left behind after a move would quietly widen the boundary."""
    missing = [rel for rel in EXEMPT if not (ROOT / rel).is_file()]
    assert missing == [], f"stale exemptions: {missing}"
