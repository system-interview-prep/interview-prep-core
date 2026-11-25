from src.infrastructure.database import SessionFactory
from src.modules.interviews.application.background.question_suggestions import (
    SUGGEST_TASK_NAME,
    suggest_questions_for_closed_session,
)
from src.modules.interviews.question_bank.coverage import (
    PREPARE_TASK_NAME,
    prepare_job_question_coverage,
)
from src.workers.async_runner import worker_async_runner
from src.workers.celery_app import celery_app


# No autoretry: the task is idempotent (the next publish or a backfill catches
# up) and every retry would pay for LLM calls again.
@celery_app.task(name=PREPARE_TASK_NAME)
def prepare_question_coverage(payload: dict | None = None) -> dict:
    job_ids = (payload or {}).get("job_ids") or None
    return worker_async_runner.run(_prepare_question_coverage(job_ids))


async def _prepare_question_coverage(job_ids: list[str] | None) -> dict:
    async with SessionFactory() as db:
        return await prepare_job_question_coverage(db, job_ids=job_ids)


@celery_app.task(name=SUGGEST_TASK_NAME)
def suggest_session_question_gaps(payload: dict | None = None) -> dict:
    session_id = str((payload or {}).get("session_id") or "").strip()
    if not session_id:
        return {"status": "SKIPPED", "reason": "missing_session_id"}
    return worker_async_runner.run(_suggest_session_question_gaps(session_id))


async def _suggest_session_question_gaps(session_id: str) -> dict:
    async with SessionFactory() as db:
        return await suggest_questions_for_closed_session(db, session_id=session_id)
