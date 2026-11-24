"""Sample job descriptions: IT postings collected from TopCV (dev fixtures)."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.job_descriptions.ingestion.models import ExternalJobCandidate, IngestionConfig
from src.modules.job_descriptions.ingestion.repository import JobIngestionRepository
from src.modules.job_descriptions.ingestion.service import JobIngestionService
from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser
from src.modules.taxonomy.facade import load_active_skill_taxonomy

_DATA_FILE = Path(__file__).parent / "data" / "topcv_it_jobs.json"

# ck_job_descriptions_source_type has no TopCV value; 'other' is the catch-all.
_SOURCE_TYPE = "other"
_BOARD_TOKEN = "topcv-it-seed"


def load_job_description_fixtures() -> list[ExternalJobCandidate]:
    jobs = json.loads(_DATA_FILE.read_text(encoding="utf-8"))["jobs"]
    return [
        ExternalJobCandidate(
            external_job_id=job["external_job_id"],
            title=job["title"],
            company_name=job["company_name"],
            company_logo_url=job["company_logo_url"],
            location=job["location"],
            raw_html=job["raw_html"],
            source_type=_SOURCE_TYPE,
            source_key=f"tenant:{_BOARD_TOKEN}",
            source_name="TopCV",
            source_url=job["source_url"],
            apply_url=job["source_url"],
            posted_at=datetime.fromisoformat(job["posted_at"]).replace(tzinfo=UTC),
        )
        for job in jobs
    ]


async def seed_job_descriptions(session_factory: Callable[[], AsyncSession]) -> dict[str, int]:
    """Idempotently seed the sample jobs through the external-job ingestion path."""
    config = IngestionConfig(board_token=_BOARD_TOKEN, company_name="TopCV", source_type=_SOURCE_TYPE)

    async with session_factory() as session:
        taxonomy = await load_active_skill_taxonomy(session)
        parser = DeterministicJobDescriptionParser(taxonomy.skills, taxonomy.version)
        service = JobIngestionService(JobIngestionRepository(session), parser=parser)
        summary = await service.ingest_candidates(config, load_job_description_fixtures())
        await session.commit()

    return summary.metrics
