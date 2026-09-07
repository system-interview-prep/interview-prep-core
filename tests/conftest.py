from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


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
