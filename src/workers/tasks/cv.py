import asyncio
import json

from sqlalchemy import text

from src.infrastructure.database import SessionFactory
from src.infrastructure.r2 import get_object, put_object
from src.modules.user_cvs.parsing.deterministic import DeterministicResumeParser
from src.modules.user_cvs.parsing.source import build_source_document
from src.workers.celery_app import celery_app
from src.workers.mineru import extract_document_artifacts


@celery_app.task(name="cv.parse", bind=True)
def parse_cv(self, payload: dict) -> dict:
    return asyncio.run(_parse_cv(str(payload.get("cv_id") or "")))


async def _parse_cv(cv_id: str) -> dict:
    if not cv_id:
        return {"status": "ignored"}
    async with SessionFactory() as db:
        await db.execute(
            text("UPDATE user_cvs SET status = 'PARSING', error = NULL, updated_at = now() WHERE id = :id"),
            {"id": cv_id},
        )
        await db.commit()
        row = (
            (
                await db.execute(
                    text("SELECT filename, storage_key, checksum FROM user_cvs WHERE id = :id"),
                    {"id": cv_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if not row:
            return {"status": "missing"}
        try:
            artifacts = await extract_document_artifacts(
                get_object(row["storage_key"]), row["filename"], cv_id
            )
            if not artifacts.markdown.strip():
                raise ValueError("MinerU returned no extractable text")
            artifact_key = f"{row['storage_key']}.mineru.json"
            put_object(
                artifact_key,
                json.dumps(artifacts.as_dict(), ensure_ascii=False).encode("utf-8"),
                "application/json",
            )
            source = build_source_document(
                artifacts,
                document_id=cv_id,
                document_sha256=row["checksum"],
            )
            parsed = DeterministicResumeParser().parse(
                source,
                extraction_version=artifacts.extractor_version or "mineru-unknown",
                source_artifact_key=artifact_key,
            )
            await db.execute(
                text(
                    "UPDATE user_cvs SET status = 'DONE', raw_text = :raw_text, "
                    "parsed_data = CAST(:parsed_data AS jsonb), "
                    "parse_source = 'mineru+deterministic-v1', updated_at = now() WHERE id = :id"
                ),
                {
                    "id": cv_id,
                    "raw_text": source.text,
                    "parsed_data": parsed.resume.model_dump_json(by_alias=True),
                },
            )
            await db.commit()
            return {
                "status": "DONE",
                "cv_id": cv_id,
                "canonical_status": parsed.resume.parsing.status if parsed.resume.parsing else None,
            }
        except Exception as exc:
            await db.execute(
                text(
                    "UPDATE user_cvs SET status = 'FAILED', error = :error, updated_at = now() WHERE id = :id"
                ),
                {"id": cv_id, "error": str(exc)[:1000]},
            )
            await db.commit()
            return {"status": "FAILED", "cv_id": cv_id}
