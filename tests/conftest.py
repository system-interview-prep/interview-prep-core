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
