import asyncio

from sqlalchemy import text

from src.infrastructure.database import SessionFactory
from src.infrastructure.r2 import get_object
from src.workers.celery_app import celery_app
from src.workers.mineru import extract_markdown


@celery_app.task(name="job_description.parse", bind=True)
def parse_job_description(self, payload: dict) -> dict:
    return asyncio.run(_parse_job_description(str(payload.get("upload_id") or "")))


async def _parse_job_description(upload_id: str) -> dict:
    if not upload_id:
        return {"status": "ignored"}
    async with SessionFactory() as db:
        await db.execute(
            text(
                "UPDATE job_descriptions SET status = 'PARSING', error = NULL, updated_at = now() "
                "WHERE id = :id AND item_type = 'JD_UPLOAD'"
            ),
            {"id": upload_id},
        )
        await db.commit()
        row = (
            (
                await db.execute(
                    text(
                        "SELECT filename, storage_key FROM job_descriptions "
                        "WHERE id = :id AND item_type = 'JD_UPLOAD'"
                    ),
                    {"id": upload_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if not row:
            return {"status": "missing"}
        try:
            raw_text = await extract_markdown(
                get_object(row["storage_key"]), row["filename"], upload_id
            )
            if not raw_text.strip():
                raise ValueError("MinerU returned no extractable text")
            await db.execute(
                text(
                    "UPDATE job_descriptions SET status = 'DONE', raw_text = :raw_text, "
                    "description = :raw_text, parse_source = 'mineru', updated_at = now() WHERE id = :id"
                ),
                {"id": upload_id, "raw_text": raw_text},
            )
            await db.commit()
            return {"status": "DONE", "upload_id": upload_id}
        except Exception as exc:
            await db.execute(
                text(
                    "UPDATE job_descriptions SET status = 'FAILED', error = :error, updated_at = now() "
                    "WHERE id = :id"
                ),
                {"id": upload_id, "error": str(exc)[:1000]},
            )
            await db.commit()
            return {"status": "FAILED", "upload_id": upload_id}
