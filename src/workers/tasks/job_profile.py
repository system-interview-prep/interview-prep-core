import asyncio
from pathlib import Path

from sqlalchemy import text

from src.infrastructure.database import SessionFactory
from src.workers.celery_app import celery_app
from src.workers.mineru import extract_markdown


@celery_app.task(name="job_profile.parse", bind=True)
def parse_job_profile(self, payload: dict) -> dict:
    return asyncio.run(_parse_job_profile(str(payload.get("upload_id") or "")))


async def _parse_job_profile(upload_id: str) -> dict:
    if not upload_id:
        return {"status": "ignored"}
    async with SessionFactory() as db:
        await db.execute(
            text(
                "UPDATE job_profiles SET status = 'PARSING', error = NULL, updated_at = now() "
                "WHERE id = :id AND item_type = 'JP_UPLOAD'"
            ),
            {"id": upload_id},
        )
        await db.commit()
        row = (
            await db.execute(
                text("SELECT filename FROM job_profiles WHERE id = :id AND item_type = 'JP_UPLOAD'"),
                {"id": upload_id},
            )
        ).mappings().one_or_none()
        if not row:
            return {"status": "missing"}
        path = Path("/app/data/job-profiles") / f"{upload_id}{Path(row['filename']).suffix.lower()}"
        try:
            if not path.is_file():
                raise FileNotFoundError("Uploaded JD file is unavailable to the worker")
            raw_text = await extract_markdown(path, row["filename"], upload_id)
            if not raw_text.strip():
                raise ValueError("MinerU returned no extractable text")
            await db.execute(
                text(
                    "UPDATE job_profiles SET status = 'DONE', raw_jd_text = :raw_text, "
                    "description = :raw_text, parse_source = 'mineru', updated_at = now() WHERE id = :id"
                ),
                {"id": upload_id, "raw_text": raw_text},
            )
            await db.commit()
            return {"status": "DONE", "upload_id": upload_id}
        except Exception as exc:
            await db.execute(
                text(
                    "UPDATE job_profiles SET status = 'FAILED', error = :error, updated_at = now() "
                    "WHERE id = :id"
                ),
                {"id": upload_id, "error": str(exc)[:1000]},
            )
            await db.commit()
            return {"status": "FAILED", "upload_id": upload_id}
