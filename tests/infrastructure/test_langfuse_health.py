"""Langfuse reachability probe (src/infrastructure/langfuse/health.py)."""

from __future__ import annotations

from src.infrastructure.langfuse.health import langfuse_status


def test_llm_connections_are_informational() -> None:
    assert langfuse_status(None, None) == ("down", "unreachable")
    # Organization connections are invisible to the project API: no false alarm.
    assert langfuse_status("https://lf/project/p", []) == ("ok", None)
    assert langfuse_status("https://lf/project/p", ["openai"]) == ("ok", None)
