import asyncio
from sqlalchemy import text

from src.infrastructure.database import SessionFactory
from src.infrastructure.r2 import get_object
from src.workers.celery_app import celery_app
from src.workers.mineru import extract_markdown


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
        row = (await db.execute(text("SELECT filename, s3_key FROM user_cvs WHERE id = :id"), {"id": cv_id})).mappings().one_or_none()
        if not row:
            return {"status": "missing"}
        try:
            raw_text = await extract_markdown(get_object(row["s3_key"]), row["filename"], cv_id)
            if not raw_text.strip():
                raise ValueError("MinerU returned no extractable text")
            await db.execute(
                text(
                    "UPDATE user_cvs SET status = 'DONE', raw_text = :raw_text, "
                    "parse_source = 'mineru', updated_at = now() WHERE id = :id"
                ),
                {"id": cv_id, "raw_text": raw_text},
            )
            await db.commit()
            return {"status": "DONE", "cv_id": cv_id}
        except Exception as exc:
            await db.execute(
                text("UPDATE user_cvs SET status = 'FAILED', error = :error, updated_at = now() WHERE id = :id"),
                {"id": cv_id, "error": str(exc)[:1000]},
            )
            await db.commit()
            return {"status": "FAILED", "cv_id": cv_id}
