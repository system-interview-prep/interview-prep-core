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
from src.workers.async_runner import worker_async_runner
from src.workers.celery_app import celery_app


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
    return worker_async_runner.run(
        _parse_job_description(
            str(payload.get("upload_id") or ""),
            str(payload.get("version_id") or "") or None,
        )
    )


async def _parse_job_description(upload_id: str, version_id: str | None = None) -> dict:
    if not upload_id:
        return {"status": "ignored"}
    try:
        async with SessionFactory() as db:
            if version_id:
                from sqlalchemy import text

                await db.execute(
                    text(
                        "UPDATE job_description_versions SET processing_status = 'PROCESSING', error = NULL "
                        "WHERE id = CAST(:version_id AS uuid) AND source_upload_id = :upload_id"
                    ),
                    {"version_id": version_id, "upload_id": upload_id},
                )
                await db.commit()
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
            if version_id and result.status == "DONE":
                await db.execute(
                    text(
                        "UPDATE job_description_versions v SET raw_text = u.raw_text, "
                        "structured_data = u.structured_data, extracted_metadata = u.extracted_metadata, "
                        "processing_status = 'DONE', error = NULL "
                        "FROM job_descriptions u WHERE v.id = CAST(:version_id AS uuid) "
                        "AND v.source_upload_id = u.id AND u.id = :upload_id"
                    ),
                    {"version_id": version_id, "upload_id": upload_id},
                )
                await db.commit()
            response = {"status": result.status, "upload_id": result.upload_id}
            if version_id:
                response["version_id"] = version_id
            return response
    except Exception as exc:
        try:
            async with SessionFactory() as err_db:
                repo = SqlAlchemyJobDescriptionParseRepository(err_db)
                await repo.fail(upload_id, f"Parse error: {exc}"[:1000])
                if version_id:
                    from sqlalchemy import text

                    await err_db.execute(
                        text(
                            "UPDATE job_description_versions SET processing_status = 'FAILED', "
                            "error = :error WHERE id = CAST(:version_id AS uuid)"
                        ),
                        {"version_id": version_id, "error": f"Parse error: {exc}"[:1000]},
                    )
                    await err_db.commit()
        except Exception:
            pass
        raise
