from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.modules.health.router import interview_readiness


def test_health_returns_service_identity(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "interview-prep-fastapi-backend"}


def test_health_rejects_unsupported_method(client) -> None:
    assert client.post("/health").status_code == 405


@pytest.mark.asyncio
async def test_interview_readiness_checks_database() -> None:
    db = AsyncMock()

    result = await interview_readiness(db)

    db.execute.assert_awaited_once()
    assert result["status"] == "ready"
    assert result["database"] == "ok"
    assert isinstance(result["livekitConfigured"], bool)


@pytest.mark.asyncio
async def test_interview_readiness_returns_503_when_database_is_down() -> None:
    db = AsyncMock()
    db.execute.side_effect = RuntimeError("database unavailable")

    with pytest.raises(HTTPException) as exc_info:
        await interview_readiness(db)

    assert exc_info.value.status_code == 503
