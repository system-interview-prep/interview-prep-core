"""Unit tests for BM25 providers (InMemory and ParadeDB)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.modules.matching.retrieval.bm25_provider import (
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
    mock_mappings.one_or_none.return_value = {"indexed": True, "bm25_score": 15.0}
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


def _factory_returning(row):
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_mappings = MagicMock()
    mock_mappings.one_or_none.return_value = row
    mock_result.mappings.return_value = mock_mappings
    mock_session.execute.return_value = mock_result

    class MockSessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    return MockSessionFactory()


@pytest.mark.asyncio
async def test_an_indexed_cv_that_shares_no_term_scores_zero():
    """Searched, and nothing matched. That is evidence, so it is a real 0.0."""
    provider = ParadeDbBm25Provider(
        session_factory=_factory_returning({"indexed": True, "bm25_score": None})
    )
    score = await provider.score_async(
        query_text="Rust Systems Engineer",
        doc_text="Java Developer",
        doc_id="cv_456",
    )

    assert score == 0.0


@pytest.mark.asyncio
async def test_a_cv_with_no_indexed_text_is_unknown_not_zero():
    """Nothing was searched, so 0.0 would assert an overlap nobody measured."""
    provider = ParadeDbBm25Provider(
        session_factory=_factory_returning({"indexed": False, "bm25_score": None})
    )
    score = await provider.score_async(
        query_text="Rust Systems Engineer",
        doc_text="Java Developer",
        doc_id="cv_456",
    )

    assert score is None


@pytest.mark.asyncio
async def test_an_unreachable_index_is_unknown_and_never_another_formula():
    """The in-memory scorer is ~4.7x lower on real pairs; substituting it would
    change the scale without the caller ever being told."""
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
    score = await provider.score_async(
        query_text="Python FastAPI",
        doc_text="Experienced with Python and FastAPI",
        doc_id="cv_789",
    )

    assert score is None


@pytest.mark.asyncio
async def test_a_resume_with_no_document_id_cannot_be_scored():
    provider = ParadeDbBm25Provider(session_factory=_factory_returning(None))
    assert await provider.score_async("Python", "Python developer", None) is None


def test_factory_returns_expected_types():
    assert isinstance(get_bm25_provider("in_memory"), InMemoryBm25Provider)
    assert isinstance(get_bm25_provider("paradedb"), ParadeDbBm25Provider)
    assert isinstance(get_bm25_provider("auto"), ParadeDbBm25Provider)
