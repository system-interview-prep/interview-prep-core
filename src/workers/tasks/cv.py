from contextlib import nullcontext

from src.core.config import get_settings
from src.infrastructure.database import SessionFactory
from src.modules.taxonomy.service import load_active_skill_taxonomy
from src.modules.user_cvs.application import run_tracking
from src.modules.user_cvs.parsing.application.pipeline import CvParsingPipeline
from src.modules.user_cvs.parsing.domain.deterministic import DeterministicResumeParser
from src.modules.user_cvs.parsing.domain.hybrid import HybridResumeParser
from src.modules.user_cvs.parsing.domain.source import build_source_document
from src.modules.user_cvs.parsing.infrastructure.extractor_factory import build_document_extractor
from src.modules.user_cvs.parsing.infrastructure.repository import SqlAlchemyCvParseRepository
from src.modules.user_cvs.parsing.infrastructure.storage import R2ObjectStorage
from src.workers.async_runner import worker_async_runner
from src.workers.celery_app import celery_app


@celery_app.task(
    name="cv.parse",
    bind=True,
    autoretry_for=(TimeoutError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def parse_cv(self, payload: dict) -> dict:
    # Only the last attempt may mark the CV FAILED; earlier timeouts are retried.
    final_attempt = self.request.retries >= self.max_retries
    attempt = int(self.request.retries or 0) + 1
    return worker_async_runner.run(
        _parse_cv(str(payload.get("cv_id") or ""), final_attempt=final_attempt, attempt=attempt)
    )


async def _parse_cv(cv_id: str, *, final_attempt: bool = True, attempt: int | None = None) -> dict:
    mode = get_settings().cv_parser_mode.casefold().strip()
    # One parse attempt of the admin CV-parsing view; the pipeline reuses it.
    tracking = run_tracking.cv_run(cv_id, attempt=attempt, parser_mode=mode) if cv_id else nullcontext()
    with tracking:
        return await _parse_cv_in_run(cv_id, mode=mode, final_attempt=final_attempt)


async def _parse_cv_in_run(cv_id: str, *, mode: str, final_attempt: bool) -> dict:
    async with SessionFactory() as session:
        taxonomy = await load_active_skill_taxonomy(session)
        if mode == "hybrid":
            parser = HybridResumeParser(taxonomy=taxonomy.skills, taxonomy_version=taxonomy.version)
        elif mode == "deterministic":
            parser = DeterministicResumeParser(taxonomy=taxonomy.skills, taxonomy_version=taxonomy.version)
        else:
            raise ValueError("CV_PARSER_MODE must be 'deterministic' or 'hybrid'")
        pipeline = CvParsingPipeline(
            repository=SqlAlchemyCvParseRepository(session),
            storage=R2ObjectStorage(),
            extractor=build_document_extractor(),
            parser=parser,
            source_builder=build_source_document,
        )
        result = await pipeline.run(cv_id, final_attempt=final_attempt)
        return {
            "status": result.status,
            "cv_id": result.cv_id,
            "canonical_status": result.canonical_status,
        }
