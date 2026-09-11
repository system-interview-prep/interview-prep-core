from src.infrastructure.database import SessionFactory
from src.modules.user_cvs.parsing.application.pipeline import CvParsingPipeline
from src.modules.user_cvs.parsing.domain.deterministic import DeterministicResumeParser
from src.modules.user_cvs.parsing.domain.source import build_source_document
from src.modules.user_cvs.parsing.infrastructure.mineru_adapter import MinerUDocumentExtractor
from src.modules.user_cvs.parsing.infrastructure.repository import SqlAlchemyCvParseRepository
from src.modules.user_cvs.parsing.infrastructure.storage import R2ObjectStorage
from src.modules.taxonomy.service import load_active_skill_taxonomy
from src.workers.celery_app import celery_app
from src.workers.async_runner import WorkerEventLoopRunner

_async_runner = WorkerEventLoopRunner()


@celery_app.task(
    name="cv.parse",
    bind=True,
    autoretry_for=(TimeoutError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def parse_cv(self, payload: dict) -> dict:
    del self
    return _async_runner.run(_parse_cv(str(payload.get("cv_id") or "")))


async def _parse_cv(cv_id: str) -> dict:
    async with SessionFactory() as session:
        taxonomy = await load_active_skill_taxonomy(session)
        pipeline = CvParsingPipeline(
            repository=SqlAlchemyCvParseRepository(session),
            storage=R2ObjectStorage(),
            extractor=MinerUDocumentExtractor(),
            parser=DeterministicResumeParser(taxonomy=taxonomy.skills, taxonomy_version=taxonomy.version),
            source_builder=build_source_document,
        )
        result = await pipeline.run(cv_id)
        return {
            "status": result.status,
            "cv_id": result.cv_id,
            "canonical_status": result.canonical_status,
        }
