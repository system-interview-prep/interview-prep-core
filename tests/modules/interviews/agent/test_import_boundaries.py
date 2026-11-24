"""T1.0.4 — import direction of the interview packages (INTERVIEW_AGENT_DECISION_PLAN_V1 §Luật phụ thuộc).

    api, adapters ─► application ─► agent ─► core ─► ai.facade

``agent/interviewer/`` decides without database access. The bounded Planner P1/P2
tools own their preparation persistence; ``core/`` executes and must not know
LangGraph or the agent; ``prompt/`` only builds strings.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4] / "src" / "modules" / "interviews"

RULES = {
    "agent": (
        "sqlalchemy", "psycopg",  # shared/checkpoints.py is the one exception, below
        "src.modules.interviews.application",
        "src.modules.interviews.adapters",
        "src.modules.interviews.evaluation",
        "src.modules.interviews.api",
    ),
    "core": (
        "langgraph", "langmem", "sqlalchemy",
        "src.modules.interviews.agent",
        "src.modules.interviews.application",
        "src.modules.interviews.adapters",
    ),
    "core/conversation/prompt": (
        "src.modules.interviews.core.conversation.turn_engine",
        "src.modules.interviews.core.conversation.response_generation",
        "src.modules.interviews.core.conversation.turn_rules",
        "src.modules.ai",
    ),
}
# Checkpoint storage and the Planner's bounded P1/P2 tools talk to persistence
# by design. Other agent modules retain the no-database boundary.
EXEMPT = {
    "agent/shared/checkpoints.py": ("psycopg",),
    "agent/planner/planner.py": (
        "sqlalchemy",
        "src.modules.interviews.application",
    ),
    "agent/planner/question_selector.py": ("sqlalchemy",),
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
