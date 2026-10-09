"""Ingest 10 hand-picked, live Greenhouse postings (with embedded logos) for demos.

Usage (from the repo root, with the docker stack up)::

    python scripts/demo_jobs/ingest_demo_jobs.py            # dry run
    python scripts/demo_jobs/ingest_demo_jobs.py --live     # write to DATABASE_URL

Postings close over time: a picked id that is no longer on its board is
reported as missing and skipped. Logos are stored as data: URIs so the job
cards do not depend on third-party hotlinking.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

from src.modules.job_descriptions.ingestion.adapters.greenhouse import (  # noqa: E402
    GreenhouseJobBoardAdapter,
)
from src.modules.job_descriptions.ingestion.models import IngestionConfig  # noqa: E402

# board token, company name, logo file, Greenhouse job ids (picked 2026-10-09)
PICKS = [
    ("axon", "Axon", "axon.png", ["7806690003", "5753033003"]),
    ("thoughtworks", "Thoughtworks", "thoughtworks.png", ["7920282"]),
    ("agoda", "Agoda", "agoda.png", ["8260757"]),
    ("stripe", "Stripe", "stripe.png", ["7964956"]),
    ("okx", "OKX", "okx.png", ["7777043003"]),
    ("gitlab", "GitLab", "gitlab.png", ["8689243002"]),
    ("reddit", "Reddit", "reddit.png", ["8194576"]),
    ("airbnb", "Airbnb", "airbnb.png", ["8249626"]),
    ("vercel", "Vercel", "vercel.png", ["5430088004"]),
]


def _logo_data_uri(name: str) -> str:
    return "data:image/png;base64," + base64.b64encode((_HERE / "logos" / name).read_bytes()).decode()


async def main(live: bool) -> int:
    adapter = GreenhouseJobBoardAdapter()
    batches = []
    for token, company, logo, ids in PICKS:
        config = IngestionConfig(
            board_token=token, company_name=company, company_logo_url=_logo_data_uri(logo)
        )
        jobs = {c.external_job_id: c for c in await adapter.fetch_board_jobs(config)}
        picked = [jobs[i] for i in ids if i in jobs]
        missing = [i for i in ids if i not in jobs]
        print(f"{company}: picked {len(picked)}" + (f", no longer open: {missing}" if missing else ""))
        for cand in picked:
            print(f"   - {cand.title} | {cand.location}")
        batches.append((config, picked))

    if not live:
        print("Dry run: nothing written. Pass --live to ingest.")
        return 0

    from src.infrastructure.database import SessionFactory
    from src.modules.job_descriptions.ingestion.repository import JobIngestionRepository
    from src.modules.job_descriptions.ingestion.service import JobIngestionService
    from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser
    from src.modules.taxonomy.facade import load_active_skill_taxonomy

    # Parse with the same database taxonomy the upload worker uses.
    async with SessionFactory() as session:
        taxonomy = await load_active_skill_taxonomy(session)
    from src.core.config import get_settings
    from src.modules.job_descriptions.parsing.hybrid import HybridJobDescriptionParser

    parser_cls = (
        HybridJobDescriptionParser
        if get_settings().jd_parser_mode.casefold().strip() == "hybrid"
        else DeterministicJobDescriptionParser
    )
    parser = parser_cls(taxonomy.skills, taxonomy.version)

    status = 0
    for config, picked in batches:
        # Only the picked ids are passed, so the close-absent sweep would close
        # other postings of the same board ingested earlier; demo boards hold
        # only these picks.
        async with SessionFactory() as session, session.begin():
            service = JobIngestionService(JobIngestionRepository(session), adapter, parser)
            summary = await service.ingest_candidates(config, picked)
        print(config.company_name, summary.status, summary.metrics, summary.errors or "")
        status |= summary.status != "COMPLETED"
    return status


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--live" in sys.argv)))
