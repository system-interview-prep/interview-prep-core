from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _no_live_question_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings read `.env`, which may hold a real OPENAI_API_KEY: never let the
    selector's question generator reach the provider from a test. Tests that
    exercise generation monkeypatch `generate_question_payloads` themselves."""
    from src.modules.interviews.planning import question_generation

    async def _no_payloads(**_: object) -> list[dict]:
        return []

    monkeypatch.setattr(question_generation, "generate_question_payloads", _no_payloads)

    # The quality gate's judge is a second LLM call: approve everything by
    # default; tests of the gate patch `judge_question_payloads` themselves.
    async def _approve_all(*, questions: list[dict], **_: object) -> list[dict]:
        checks = ("on_skill", "difficulty_ok", "verbal", "sound", "concise")
        return [{"index": index, **dict.fromkeys(checks, True)} for index in range(len(questions))]

    monkeypatch.setattr(question_generation, "judge_question_payloads", _approve_all)

    # Same for matching's LLM evidence search: with a real key it made
    # test_targeted_reparse pass or fail depending on the model's answer.
    from src.modules.matching.application import targeted_reparse

    monkeypatch.setattr(targeted_reparse, "_default_evidence_searcher", lambda: None)


@pytest.fixture(autouse=True)
def _trace_logs_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Keep test traces out of the developer's trace-logs/.

    With TRACE_LOGS_ENABLED=true in `.env`, every test that ran an interview
    turn appended to trace-logs/naturalness/trace.jsonl: on 24/10, 320 of its
    340 lines came from tests (sessions "sess-123"…), which skewed the
    naturalness numbers read from real sessions.
    """
    from src.core.config import get_settings

    monkeypatch.setattr(get_settings(), "trace_logs_dir", str(tmp_path_factory.mktemp("trace-logs")))


@pytest.fixture
def client() -> Iterator[TestClient]:
    from src.main import create_app

    # Do not enter TestClient as a context manager: API contract tests do not need
    # RabbitMQ lifespan. Broker behavior is mocked at the router boundary.
    client = TestClient(create_app())
    try:
        yield client
    finally:
        client.close()
