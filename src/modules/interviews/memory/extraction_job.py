"""Post-session memory extraction as a durable background job (V2 spec §9).

The grader persists the report and commits; only then is extraction
scheduled, on the Celery/RabbitMQ worker the project already runs. Nothing
here can touch the grade: a failed, duplicated or lost extraction leaves the
evaluation exactly as the grader wrote it.

What makes the job safe to deliver more than once and to lose a worker on:

* **Idempotency** -- a per-user marker in the memory store records the
  session as ``DONE`` with the counts; a redelivered task sees it and stops.
  The marker lives beside the memory it describes, in the same namespace
  rules (per user), so no new table is needed.
* **Mutual exclusion** -- a transaction-scoped Postgres advisory lock on the
  session id. A second worker that picks up the same session while one is
  still extracting does not run a second extraction; it asks to be retried.
* **Worker failure** -- Celery acknowledges late and rejects on worker loss
  (``celery_app.py``), so a crashed worker's task is redelivered; the
  advisory lock died with the crashed connection, and the marker is still
  ``PROCESSING`` or ``FAILED``, so the next delivery runs it again.
* **Dispatch failure** -- :func:`enqueue_memory_extraction` returns ``False``
  when the broker refuses; the caller keeps the pre-§9 inline extraction for
  that case, so memory is not silently lost and the report is returned either
  way.

The job is at-least-once, not exactly-once: the lock and the marker are two
mechanisms, not one atomic one. A worker that dies between finishing the
extraction and writing ``DONE`` causes one more extraction on redelivery;
``candidate_memory`` caps every run at ``MAX_OPS`` changes and stamps items
with the session id, so a repeat is bounded and attributable.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.trace_logging import trace_event
from src.modules.interviews.memory import candidate_memory
from src.modules.interviews.memory.candidate_memory import extract_after_session

logger = logging.getLogger(__name__)

__all__ = [
    "EXTRACT_TASK_NAME",
    "JOB_NAMESPACE",
    "enqueue_memory_extraction",
    "job_marker",
    "run_memory_extraction_job",
]

#: Celery task name; routed to the default queue the worker already consumes.
EXTRACT_TASK_NAME = "interview.memory.extract_after_session"
#: Store namespace prefix of the job markers: ``(JOB_NAMESPACE, user_id)``.
JOB_NAMESPACE = "interview_memory_jobs"
#: How long a lost worker's lock is assumed to linger before a retry is useful.
RETRY_IN_SECONDS = 60


def _namespace(user_id: str) -> tuple[str, str]:
    return (JOB_NAMESPACE, str(user_id))


async def job_marker(store: Any, user_id: str, session_id: str) -> dict[str, Any] | None:
    """The job's durable record for ``session_id``, or ``None`` if never started."""
    item = await store.aget(_namespace(user_id), str(session_id))
    if item is None:
        return None
    value = getattr(item, "value", item)
    return dict(value) if isinstance(value, dict) else None


async def _mark(store: Any, user_id: str, session_id: str, **fields: Any) -> None:
    await store.aput(
        _namespace(user_id),
        str(session_id),
        {"session_id": str(session_id), "updated_at": datetime.now(UTC).isoformat(), **fields},
    )


async def _try_lock(db: AsyncSession, session_id: str) -> bool:
    """A transaction-scoped advisory lock on the session; released at commit/rollback."""
    locked = await db.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
        {"key": f"interview-memory:{session_id}"},
    )
    return bool(locked)


async def run_memory_extraction_job(
    db: AsyncSession,
    session_id: str,
    *,
    store: Any,
    evaluation: dict[str, Any] | None,
    attempt: int = 1,
) -> dict[str, Any]:
    """Extract memory for one closed, graded session; safe to call repeatedly.

    ``evaluation`` is the persisted report (the worker loads it with
    ``get_session_evaluation``); ``store`` is the memory store the worker
    opened. Returns a status dict and **raises** when the extraction itself
    fails, so Celery retries it with backoff. ``RETRY`` asks the caller to
    try again later without counting it as a failure.
    """
    session_id = str(session_id)
    if candidate_memory.enabled() != "readwrite":
        return {"sessionId": session_id, "status": "SKIPPED", "reason": "memory_disabled"}
    if store is None:
        return {"sessionId": session_id, "status": "SKIPPED", "reason": "store_unavailable"}
    if not evaluation:
        # The report is persisted before dispatch, so this is a session whose
        # evaluation was deleted or never finished -- nothing to extract from.
        return {"sessionId": session_id, "status": "SKIPPED", "reason": "evaluation_not_available"}
    user_id = await candidate_memory._user_id_of_session(db, session_id)
    if not user_id:
        return {"sessionId": session_id, "status": "SKIPPED", "reason": "no_user"}

    marker = await job_marker(store, user_id, session_id)
    if marker and marker.get("status") == "DONE":
        trace_event("interviewer", "candidate_memory_job_duplicate", session_id=session_id)
        return {"sessionId": session_id, "status": "SKIPPED", "reason": "already_extracted", "counts": marker.get("counts")}

    if not await _try_lock(db, session_id):
        trace_event("interviewer", "candidate_memory_job_busy", session_id=session_id)
        return {"sessionId": session_id, "status": "RETRY", "reason": "in_progress", "retry_in": RETRY_IN_SECONDS}

    await _mark(store, user_id, session_id, status="PROCESSING", attempt=attempt, started_at=datetime.now(UTC).isoformat())
    try:
        counts = await extract_after_session(db, session_id, evaluation=evaluation, store=store, raise_errors=True)
    except Exception as exc:
        await _mark(store, user_id, session_id, status="FAILED", attempt=attempt, error=f"{type(exc).__name__}: {exc}"[:500])
        raise
    await _mark(store, user_id, session_id, status="DONE", attempt=attempt, counts=dict(counts or {}))
    return {"sessionId": session_id, "status": "COMPLETE", "counts": dict(counts or {})}


def enqueue_memory_extraction(session_id: str) -> bool:
    """Queue extraction after a persisted report; broker errors never invalidate scoring."""
    if candidate_memory.enabled() != "readwrite":
        return False
    try:
        from src.workers.celery_app import celery_app

        celery_app.send_task(EXTRACT_TASK_NAME, args=[{"session_id": str(session_id)}])
        trace_event("interviewer", "candidate_memory_job_enqueued", session_id=str(session_id))
        return True
    except Exception:
        logger.exception("Could not enqueue memory extraction for %s", session_id)
        trace_event("interviewer", "candidate_memory_job_enqueue_failed", session_id=str(session_id))
        return False


def max_attempts() -> int:
    return int(get_settings().rabbitmq_max_attempts)
