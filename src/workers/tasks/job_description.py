import json

from sqlalchemy import text

from src.core.config import get_settings
from src.infrastructure.database import SessionFactory
from src.infrastructure.r2 import get_object, put_object
from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser
from src.modules.job_descriptions.parsing.hybrid import HybridJobDescriptionParser
from src.modules.user_cvs.parsing.domain.source import build_source_document
from src.modules.taxonomy.service import load_active_skill_taxonomy
from src.workers.async_runner import WorkerEventLoopRunner
from src.workers.celery_app import celery_app
from src.workers.mineru import extract_document_artifacts

_async_runner = WorkerEventLoopRunner()


@celery_app.task(name="job_description.parse", bind=True, autoretry_for=(TimeoutError,), retry_backoff=True, retry_jitter=True, max_retries=3)
def parse_job_description(self, payload: dict) -> dict:
    del self
    return _async_runner.run(_parse_job_description(str(payload.get("upload_id") or "")))


async def _parse_job_description(upload_id: str) -> dict:
    if not upload_id:
        return {"status": "ignored"}
    async with SessionFactory() as db:
        taxonomy = await load_active_skill_taxonomy(db)
        await db.execute(text("UPDATE job_descriptions SET status = 'PARSING', error = NULL, updated_at = now() WHERE id = :id AND item_type = 'JD_UPLOAD'"), {"id": upload_id})
        await db.commit()
        row = (await db.execute(text("SELECT filename, storage_key, checksum FROM job_descriptions WHERE id = :id AND item_type = 'JD_UPLOAD'"), {"id": upload_id})).mappings().one_or_none()
        if not row:
            return {"status": "missing"}
        try:
            artifacts = await extract_document_artifacts(get_object(row["storage_key"]), row["filename"], upload_id)
            source = build_source_document(artifacts, document_id=upload_id, document_sha256=row["checksum"])
            artifact_key = f"{row['storage_key']}.artifacts/{row['checksum']}/mineru.json"
            put_object(artifact_key, json.dumps(artifacts.as_dict()).encode(), "application/json")
            extraction_version = artifacts.extractor_version or "mineru-unknown"
            mode = get_settings().jd_parser_mode.casefold().strip()
            if mode == "hybrid":
                parsed = await HybridJobDescriptionParser(taxonomy.skills, taxonomy.version).parse(
                    source, extraction_version=extraction_version, artifact_key=artifact_key
                )
            elif mode == "deterministic":
                parsed = DeterministicJobDescriptionParser(taxonomy.skills, taxonomy.version).parse(
                    source, extraction_version=extraction_version, artifact_key=artifact_key
                )
            else:
                raise ValueError("JD_PARSER_MODE must be 'deterministic' or 'hybrid'")
            await db.execute(text("UPDATE job_descriptions SET status = 'DONE', raw_text = :raw_text, description = :raw_text, structured_data = CAST(:structured_data AS jsonb), parse_source = :parse_source, updated_at = now() WHERE id = :id"), {"id": upload_id, "raw_text": source.text, "structured_data": parsed.model_dump_json(by_alias=True), "parse_source": f"mineru+{parsed.parsing.parser_version}"})
            await db.commit()
            return {"status": "DONE", "upload_id": upload_id}
        except Exception as exc:
            await db.execute(text("UPDATE job_descriptions SET status = 'FAILED', error = :error, updated_at = now() WHERE id = :id"), {"id": upload_id, "error": str(exc)[:1000]})
            await db.commit()
            raise
