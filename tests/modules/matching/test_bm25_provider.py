"""Unit tests for BM25 providers (InMemory and ParadeDB)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.modules.matching.bm25_provider import (
    InMemoryBm25Provider,
    ParadeDbBm25Provider,
    get_bm25_provider,
)


def test_in_memory_provider_scores():
    provider = InMemoryBm25Provider()
    score_match = provider.score(
        "Senior Python Developer FastAPI Docker",
        "5 years of experience with Python, building REST APIs with FastAPI and deploying on Docker.",
    )
    assert 0.0 < score_match <= 1.0

    score_unrelated = provider.score(
        "Graphic Designer Photoshop Figma",
        "Senior Java Spring Boot microservices architect.",
    )
    assert score_unrelated == 0.0


def test_paradedb_sanitize_query():
    raw_query = "Looking for Senior C++ & C# Developer with .NET and K8s (Kubernetes) exp!"
    sanitized = ParadeDbBm25Provider.sanitize_query(raw_query)
    assert "c++" in sanitized.lower()
    assert "c#" in sanitized.lower()
    assert ".net" in sanitized.lower()
    assert "kubernetes" in sanitized.lower()
    assert "(" not in sanitized
    assert ")" not in sanitized
    assert "!" not in sanitized


@pytest.mark.asyncio
async def test_paradedb_provider_score_async_match():
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_mappings = MagicMock()
    mock_mappings.one_or_none.return_value = {"bm25_score": 15.0}
    mock_result.mappings.return_value = mock_mappings
    mock_session.execute.return_value = mock_result

    class MockSessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    provider = ParadeDbBm25Provider(
        session_factory=MockSessionFactory(),
        saturation_k=15.0,
    )

    score = await provider.score_async(
        query_text="Python FastAPI",
        doc_text="Python FastAPI Developer",
        doc_id="cv_123",
    )

    # 15.0 / (15.0 + 15.0) = 0.5
    assert score == 0.5
    assert mock_session.execute.called


@pytest.mark.asyncio
async def test_paradedb_provider_score_async_no_match():
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_mappings = MagicMock()
    mock_mappings.one_or_none.return_value = None
    mock_result.mappings.return_value = mock_mappings
    mock_session.execute.return_value = mock_result

    class MockSessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    provider = ParadeDbBm25Provider(session_factory=MockSessionFactory())
    score = await provider.score_async(
        query_text="Rust Systems Engineer",
        doc_text="Java Developer",
        doc_id="cv_456",
    )

    assert score == 0.0


@pytest.mark.asyncio
async def test_paradedb_provider_fallback_on_db_exception():
    mock_session = AsyncMock()
    mock_session.execute.side_effect = ConnectionError("PostgreSQL connection refused")

    class MockSessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    provider = ParadeDbBm25Provider(session_factory=MockSessionFactory())
    # Should not raise; should gracefully fallback to InMemory provider
    score = await provider.score_async(
        query_text="Python FastAPI",
        doc_text="Experienced with Python and FastAPI",
        doc_id="cv_789",
    )

    assert score > 0.0


def test_factory_returns_expected_types():
    assert isinstance(get_bm25_provider("in_memory"), InMemoryBm25Provider)
    assert isinstance(get_bm25_provider("paradedb"), ParadeDbBm25Provider)
    assert isinstance(get_bm25_provider("auto"), ParadeDbBm25Provider)
