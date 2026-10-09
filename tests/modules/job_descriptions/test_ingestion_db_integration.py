"""Greenhouse ingestion through the real SQL repository.

The unit tests in test_pr6_ingestion.py use an in-memory repository, so they
never executed the INSERT/UPDATE statements. This runs a Greenhouse-shaped
item through adapter -> service -> JobIngestionRepository against PostgreSQL
inside a transaction that is rolled back.
"""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database import engine
from src.modules.job_descriptions.ingestion.adapters.greenhouse import GreenhouseJobBoardAdapter
from src.modules.job_descriptions.ingestion.models import IngestionConfig
from src.modules.job_descriptions.ingestion.repository import JobIngestionRepository
from src.modules.job_descriptions.ingestion.service import JobIngestionService

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_INTEGRATION_TESTS") != "1",
    reason="PostgreSQL integration database is not enabled",
)


@pytest.fixture(autouse=True)
async def _fresh_pool():
    """Each test runs on its own event loop; pooled connections must not cross loops."""
    await engine.dispose()
    yield
    await engine.dispose()


# Greenhouse returns `content` entity-escaped.
_CONTENT = (
    "&lt;p&gt;Reports to: Engineering Manager&lt;/p&gt;"
    "&lt;h2&gt;About You:&lt;/h2&gt;&lt;ul&gt;"
    "&lt;li&gt;3+ years of Python and Docker&lt;/li&gt;"
    "&lt;li&gt;Experience with monitoring and observability for Kotlin services&lt;/li&gt;"
    "&lt;/ul&gt;"
)


@pytest.mark.asyncio
async def test_greenhouse_job_round_trips_through_the_real_repository() -> None:
    board = f"it-{uuid4().hex[:8]}"
    config = IngestionConfig(
        board_token=board, company_name="Acme", company_logo_url="data:image/png;base64,AA=="
    )
    adapter = GreenhouseJobBoardAdapter()
    items = [
        {"id": 1, "title": "Backend Engineer", "content": _CONTENT, "updated_at": "2026-10-01T00:00:00Z"},
        {"id": 2, "title": "Old Posting", "content": "&lt;p&gt;Closed soon&lt;/p&gt;"},
    ]
    candidates = adapter.parse_board_payload({"jobs": items}, config)

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            db = AsyncSession(bind=connection, expire_on_commit=False)
            service = JobIngestionService(JobIngestionRepository(db), adapter)
            first_run = datetime.now(UTC) - timedelta(days=3)
            created = await service.ingest_candidates(config, candidates, now=first_run)
            assert created.status == "COMPLETED", created.errors
            assert created.metrics["created_count"] == 2

            row = (
                await db.execute(
                    text(
                        "SELECT description, requirements, keywords, structured_data, seniority "
                        "FROM job_descriptions WHERE source_key = :key AND external_job_id = '1'"
                    ),
                    {"key": f"tenant:{board}"},
                )
            ).mappings().one()
            assert "- 3+ years of Python and Docker" in row["description"]
            assert "&lt;" not in row["description"] and "<li>" not in row["description"]
            assert "- 3+ years of Python and Docker" in row["requirements"]
            assert {"Monitoring", "Kotlin"} <= {
                concept["label"]
                for requirement in row["structured_data"]["requirements"]
                for concept in requirement.get("atomicConcepts") or []
            }
            assert row["keywords"] is not None
            # The listing title wins over "Reports to: Engineering Manager".
            assert row["structured_data"]["jobTitle"] == "Backend Engineer"
            assert row["seniority"] is None

            # Posting 2 disappeared from the board past the grace period.
            closed = await service.ingest_candidates(config, candidates[:1])
            assert closed.status == "COMPLETED", closed.errors
            assert closed.metrics["closed_count"] == 1
        finally:
            await transaction.rollback()
