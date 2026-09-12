from src.core.config import get_settings
from src.infrastructure.database import SessionFactory
from src.modules.job_descriptions.parsing.application.pipeline import JobDescriptionParsingPipeline
from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser
from src.modules.job_descriptions.parsing.hybrid import HybridJobDescriptionParser
from src.modules.job_descriptions.parsing.infrastructure.repository import (
    SqlAlchemyJobDescriptionParseRepository,
)
from src.modules.taxonomy.service import load_active_skill_taxonomy
from src.modules.user_cvs.parsing.domain.source import build_source_document
from src.modules.user_cvs.parsing.infrastructure.mineru_adapter import MinerUDocumentExtractor
from src.modules.user_cvs.parsing.infrastructure.storage import R2ObjectStorage
from src.workers.async_runner import WorkerEventLoopRunner
from src.workers.celery_app import celery_app

_async_runner = WorkerEventLoopRunner()


@celery_app.task(
    name="job_description.parse",
    bind=True,
    autoretry_for=(TimeoutError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def parse_job_description(self, payload: dict) -> dict:
    del self
    return _async_runner.run(_parse_job_description(str(payload.get("upload_id") or "")))


async def _parse_job_description(upload_id: str) -> dict:
    if not upload_id:
        return {"status": "ignored"}
    async with SessionFactory() as db:
        taxonomy = await load_active_skill_taxonomy(db)
        mode = get_settings().jd_parser_mode.casefold().strip()
        if mode == "hybrid":
            parser = HybridJobDescriptionParser(taxonomy.skills, taxonomy.version)
        elif mode == "deterministic":
            parser = DeterministicJobDescriptionParser(taxonomy.skills, taxonomy.version)
        else:
            raise ValueError("JD_PARSER_MODE must be 'deterministic' or 'hybrid'")
        pipeline = JobDescriptionParsingPipeline(
            repository=SqlAlchemyJobDescriptionParseRepository(db),
            storage=R2ObjectStorage(),
            extractor=MinerUDocumentExtractor(),
            parser=parser,
            source_builder=build_source_document,
        )
        result = await pipeline.run(upload_id)
        return {"status": result.status, "upload_id": result.upload_id}
