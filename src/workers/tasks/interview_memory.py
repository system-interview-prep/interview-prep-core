"""Celery task: candidate-memory extraction after a session is graded (spec §9).

Runs on the worker the project already has (``celery_app.py``: acks late,
reject on worker lost, prefetch 1). The durability rules are in
``src/modules/interviews/memory/extraction_job.py``; this module only opens
the resources a worker process does not get from the app lifespan -- a
database session and a memory store -- and maps the job's outcome onto
Celery's retry semantics.
"""

from __future__ import annotations

from typing import Any

from src.infrastructure.database import SessionFactory
from src.modules.interviews.agents.evaluator.report import get_session_evaluation
from src.modules.interviews.graph.checkpoints import interview_memory_store
from src.modules.interviews.memory.extraction_job import (
    EXTRACT_TASK_NAME,
    max_attempts,
    run_memory_extraction_job,
)
from src.workers.async_runner import worker_async_runner
from src.workers.celery_app import celery_app


@celery_app.task(
    name=EXTRACT_TASK_NAME,
    bind=True,
    # An extraction that raised is retried with backoff; the job's marker and
    # advisory lock make the retry safe. Attempts are capped like the other
    # RabbitMQ consumers (settings.rabbitmq_max_attempts).
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=max_attempts(),
)
def extract_session_memory(self: Any, payload: dict | None = None) -> dict:
    session_id = str((payload or {}).get("session_id") or "").strip()
    if not session_id:
        return {"status": "SKIPPED", "reason": "missing_session_id"}
    result = worker_async_runner.run(_extract_session_memory(session_id, attempt=self.request.retries + 1))
    if result.get("status") == "RETRY":
        # Another worker holds the session; this is not a failure, so it does
        # not consume the backoff schedule -- just come back later.
        raise self.retry(countdown=int(result.get("retry_in") or 60), max_retries=None)
    return result


async def _extract_session_memory(session_id: str, *, attempt: int) -> dict:
    async with interview_memory_store() as store:
        async with SessionFactory() as db:
            evaluation = await get_session_evaluation(db, session_id)
            return await run_memory_extraction_job(
                db, session_id, store=store, evaluation=evaluation, attempt=attempt
            )
