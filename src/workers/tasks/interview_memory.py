"""Celery task: candidate-memory extraction after a session is graded (spec §9).

Runs on the worker the project already has (``celery_app.py``: acks late,
reject on worker lost, prefetch 1). The durability rules are in
``src/modules/interviews/memory/extraction_job.py``; this module opens the
resources a worker process does not get from the app lifespan -- a database
session and a memory store -- and maps the job's outcome onto Celery's retry
semantics:

* ``RETRY`` (another worker holds the session) -> ``self.retry`` after a fixed
  delay, counted against the same bounded ``max_retries``;
* :class:`TransientExtractionError` -> ``self.retry`` with the job's bounded
  exponential backoff; the job has already recorded ``FAILED`` durably;
* a permanent failure -> recorded by the job, returned as ``FAILED``; the
  task itself succeeds so Celery does not retry what cannot succeed;
* ``MaxRetriesExceededError`` cannot normally happen, because the job caps
  attempts itself; if it does, the failure is already recorded.

``max_retries`` is ``settings.rabbitmq_max_attempts - 1`` so that the total
number of attempts equals ``rabbitmq_max_attempts``, like the other consumers.
"""

from __future__ import annotations

import logging
from typing import Any

from celery.exceptions import MaxRetriesExceededError

from src.infrastructure.database import SessionFactory
from src.modules.interviews.agents.evaluator.report import get_session_evaluation
from src.modules.interviews.graph.checkpoints import MemoryStoreUnavailable, interview_memory_store
from src.modules.interviews.memory.extraction_job import (
    EXTRACT_TASK_NAME,
    TransientExtractionError,
    max_attempts,
    retry_delay_seconds,
    run_memory_extraction_job,
)
from src.workers.async_runner import worker_async_runner
from src.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def max_retries() -> int:
    return max(0, max_attempts() - 1)


def _memory_on() -> bool:
    from src.modules.interviews.memory import candidate_memory

    return candidate_memory.enabled() == "readwrite"


@celery_app.task(name=EXTRACT_TASK_NAME, bind=True, max_retries=max_retries())
def extract_session_memory(self: Any, payload: dict | None = None) -> dict:
    session_id = str((payload or {}).get("session_id") or "").strip()
    if not session_id:
        return {"status": "SKIPPED", "reason": "missing_session_id"}
    attempt = int(self.request.retries or 0) + 1
    try:
        result = worker_async_runner.run(extract_session_memory_async(session_id, attempt=attempt))
    except TransientExtractionError as exc:
        try:
            raise self.retry(exc=exc, countdown=retry_delay_seconds(attempt))
        except MaxRetriesExceededError:
            # The job records the cap itself (attempt >= max_attempts is
            # permanent), so this is only reachable if Celery's count and the
            # job's disagree; the durable state already says FAILED.
            logger.error("memory extraction for %s: retries exceeded", session_id)
            return {"sessionId": session_id, "status": "FAILED", "permanent": True, "reason": "attempts_exhausted"}
    if result.get("status") == "RETRY":
        try:
            raise self.retry(countdown=int(result.get("retry_in") or 60))
        except MaxRetriesExceededError:
            return {"sessionId": session_id, "status": "FAILED", "permanent": False, "reason": "still_in_progress"}
    return result


async def extract_session_memory_async(session_id: str, *, attempt: int) -> dict:
    """The whole job for one delivery: store, database, report, extraction."""
    async with SessionFactory() as db:
        evaluation = await get_session_evaluation(db, session_id)
        if not evaluation or not _memory_on():
            # Nothing to extract from, or memory off: the job answers without
            # opening a store (seen on the real broker: opening one costs a
            # pool round-trip, 30 s when the database is unreachable).
            return await run_memory_extraction_job(
                db, session_id, store=None, evaluation=evaluation, attempt=attempt, max_attempts=max_attempts()
            )
        try:
            async with interview_memory_store() as store:
                return await run_memory_extraction_job(
                    db, session_id, store=store, evaluation=evaluation, attempt=attempt, max_attempts=max_attempts()
                )
        except MemoryStoreUnavailable as exc:
            # Recorded durably as FAILED (permanent for a missing dependency,
            # transient for a connection problem); the job raises for a retry.
            return await run_memory_extraction_job(
                db, session_id, store=None, store_error=exc, evaluation=evaluation,
                attempt=attempt, max_attempts=max_attempts(),
            )
