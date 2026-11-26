"""Post-session memory extraction as a durable background job (V2 spec §9).

The grader persists the report and commits; only then is extraction
scheduled, on the Celery/RabbitMQ worker the project already runs. Nothing
here can touch the grade: a failed, duplicated or lost extraction leaves the
evaluation exactly as the grader wrote it.

**Job state lives in PostgreSQL**, in ``interview_sessions.metadata
->'memory_extraction'`` -- the system of record, available even when the
memory store (the thing that can fail) is not, and queryable in one SQL
statement by the reconciler. States: ``QUEUED`` (dispatched), ``PROCESSING``
(a worker is on it), ``DONE`` (with the counts), ``FAILED`` (with ``permanent``
true when no retry will help).

What makes the job safe to deliver more than once, to lose a worker on, and
to lose the dispatch of:

* **Idempotency** -- a ``DONE`` state stops a redelivery. It is read once
  before the lock (cheap exit) and **again after the lock is held**: two
  deliveries that both passed the first read cannot both extract, because the
  second to lock re-reads ``DONE``.
* **Mutual exclusion** -- a transaction-scoped Postgres advisory lock on the
  session id, held for the whole extraction (the transaction that took it is
  the one that writes ``DONE``). A worker that cannot take it asks to be
  retried later rather than failing.
* **Worker failure** -- Celery acknowledges late and rejects on worker loss
  (``celery_app.py``); the lock dies with the crashed connection; the state is
  ``PROCESSING`` and the redelivery runs it again.
* **Failure classes** -- disabled memory is a *skip*; a missing dependency
  (the ``memory`` extra) is *permanent*; a connection or provider outage is
  *transient* and retried with bounded exponential backoff; an unknown error
  is treated as transient until the attempt cap, then recorded permanent.
  A transient failure never returns a successful result.
* **Dispatch failure** -- ``score`` writes ``QUEUED`` and commits *before*
  ``send_task``. If the broker refuses, it falls back to the pre-§9 inline
  run. If the process dies between the commit and the dispatch, the
  reconciler (:func:`reconcile_memory_extraction`, run periodically by the
  API) finds evaluations whose job is neither ``DONE`` nor permanently failed
  and dispatches them again, a bounded number of times.

The job is **at-least-once, not exactly-once**: a worker lost between
finishing the extraction and writing ``DONE`` repeats one extraction on
redelivery; ``candidate_memory`` caps every run at ``MAX_OPS`` changes and
stamps items with the session id, so a repeat is bounded and attributable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.trace_logging import trace_event
from src.modules.interviews.graph.checkpoints import MemoryStoreUnavailable
from src.modules.interviews.memory import candidate_memory
from src.modules.interviews.memory.candidate_memory import extract_after_session

logger = logging.getLogger(__name__)

__all__ = [
    "EXTRACT_TASK_NAME",
    "JOB_FIELD",
    "PermanentExtractionError",
    "TransientExtractionError",
    "classify",
    "enqueue_memory_extraction",
    "job_state",
    "mark_queued",
    "max_attempts",
    "reconcile_loop",
    "reconcile_memory_extraction",
    "retry_delay_seconds",
    "run_memory_extraction_job",
]

#: Celery task name; routed to the default queue the worker already consumes.
EXTRACT_TASK_NAME = "interview.memory.extract_after_session"
#: Key inside ``interview_sessions.metadata`` holding the job state.
JOB_FIELD = "memory_extraction"
#: How long a busy session's lock is assumed to linger before a retry is useful.
RETRY_IN_SECONDS = 60
#: Exponential backoff for transient failures: 30 s, 60 s, 120 s ... capped.
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 600
#: A QUEUED/PROCESSING state younger than this is left alone by the reconciler.
LEASE_SECONDS = 900
#: The reconciler ignores evaluations younger than this (normal dispatch is in flight).
RECONCILE_GRACE_SECONDS = 120
#: ... and older than this. Short on purpose: the crash window the sweep
#: closes is seconds long, and a wide window would re-dispatch every
#: evaluation graded before this job existed -- a paid backfill nobody asked
#: for (seen on the dev database: one pre-feature session per sweep).
RECONCILE_WINDOW_HOURS = 6
#: Sessions per sweep; with the 5-minute cadence this bounds the dispatch rate.
RECONCILE_LIMIT = 20
#: Dispatches per session before the reconciler gives up for good.
MAX_DISPATCHES = 5


class TransientExtractionError(RuntimeError):
    """Retry later: the store, the database or the provider was unavailable."""


class PermanentExtractionError(RuntimeError):
    """Do not retry: nothing will change (missing dependency, attempts exhausted)."""


# ------------------------------------------------------------ classification

#: Provider errors that a retry cannot fix: the request itself is refused.
#: Seen on the real broker: a 401 was retried as if the provider were down.
_PERMANENT_MARKERS = (
    "AuthenticationError",
    "PermissionDeniedError",
    "BadRequestError",
    "NotFoundError",
    "UnprocessableEntityError",
)
_TRANSIENT_MARKERS = (
    "OperationalError",
    "InterfaceError",
    "DisconnectionError",
    "ConnectionError",
    "ConnectionRefused",
    "ConnectionReset",
    "BrokenPipe",
    "Timeout",
    "RateLimit",
    "APIConnection",
    "APIStatusError",
    "ServiceUnavailable",
    "InternalServerError",
    "TemporaryFailure",
)


def classify(exc: BaseException) -> str:
    """``"skip"``, ``"transient"`` or ``"permanent"`` for one failure.

    Disabled memory is not a failure at all and never reaches here. A missing
    dependency (``ImportError``) is permanent: no retry installs it. A request
    the provider refuses (401/403/400/404) is permanent too. Connection,
    timeout and provider-availability errors are transient. Anything else is
    *treated* as transient so a bounded retry gets a chance; the attempt cap
    turns it permanent (see :func:`run_memory_extraction_job`).
    """
    if isinstance(exc, PermanentExtractionError):
        return "permanent"
    if isinstance(exc, MemoryStoreUnavailable):
        return "permanent" if exc.permanent else "transient"
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return "permanent"
    if isinstance(exc, (TransientExtractionError, ConnectionError, TimeoutError, OSError, asyncio.TimeoutError)):
        return "transient"
    names = {klass.__name__ for klass in type(exc).__mro__}
    if any(marker in name for name in names for marker in _PERMANENT_MARKERS):
        return "permanent"
    if any(marker in name for name in names for marker in _TRANSIENT_MARKERS):
        return "transient"
    return "transient"


def retry_delay_seconds(attempt: int, *, jitter: Callable[[], float] = random.random) -> int:
    """Bounded exponential backoff with jitter for the ``attempt``-th failure (1-based)."""
    delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1)))
    return int(delay + jitter() * min(delay, 30))


def max_attempts() -> int:
    return max(1, int(get_settings().rabbitmq_max_attempts))


# ------------------------------------------------------------ job state (DB)

_READ_STATE = "SELECT metadata->'memory_extraction' FROM interview_sessions WHERE id = :sid"
_WRITE_STATE = (
    "UPDATE interview_sessions SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), "
    "'{memory_extraction}', CAST(:job AS jsonb), true), updated_at = now() WHERE id = :sid"
)


def _parse(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return dict(raw) if isinstance(raw, dict) else None


async def job_state(db: AsyncSession, session_id: str) -> dict[str, Any] | None:
    """The durable job record for ``session_id``, or ``None`` if never started."""
    return _parse(await db.scalar(text(_READ_STATE), {"sid": str(session_id)}))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _write_state(
    db: AsyncSession, session_id: str, previous: dict[str, Any] | None, **fields: Any
) -> dict[str, Any]:
    merged = {**(previous or {}), **fields, "updated_at": _now_iso()}
    await db.execute(text(_WRITE_STATE), {"sid": str(session_id), "job": json.dumps(merged, default=str)})
    return merged


def _is_done(state: dict[str, Any] | None) -> bool:
    return bool(state) and state.get("status") == "DONE"


def _is_permanent_failure(state: dict[str, Any] | None) -> bool:
    return bool(state) and state.get("status") == "FAILED" and bool(state.get("permanent"))


async def mark_queued(db: AsyncSession, session_id: str, *, reason: str = "scored") -> dict[str, Any] | None:
    """Record the dispatch durably, and commit, before the task is sent.

    Returns the new state, or ``None`` when the session is already ``DONE`` or
    permanently failed (then nothing should be dispatched).
    """
    previous = await job_state(db, session_id)
    if _is_done(previous) or _is_permanent_failure(previous):
        return None
    state = await _write_state(
        db, session_id, previous,
        status="QUEUED", reason=reason, dispatches=int((previous or {}).get("dispatches") or 0) + 1,
        queued_at=_now_iso(),
    )
    await db.commit()
    return state


async def _try_lock(db: AsyncSession, session_id: str) -> bool:
    """A transaction-scoped advisory lock on the session; released at commit/rollback."""
    locked = await db.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
        {"key": f"interview-memory:{session_id}"},
    )
    return bool(locked)


# --------------------------------------------------------------------- job


async def run_memory_extraction_job(
    db: AsyncSession,
    session_id: str,
    *,
    store: Any,
    evaluation: dict[str, Any] | None,
    attempt: int = 1,
    max_attempts: int | None = None,
    store_error: BaseException | None = None,
) -> dict[str, Any]:
    """Extract memory for one closed, graded session; safe to call repeatedly.

    ``evaluation`` is the persisted report (the worker loads it with
    ``get_session_evaluation``); ``store`` is the memory store the worker
    opened, or ``None`` with ``store_error`` saying why it could not.

    Returns a status dict for the terminal outcomes (``SKIPPED``, ``COMPLETE``,
    ``RETRY`` for a session another worker holds, and ``FAILED`` with
    ``permanent`` true). Raises :class:`TransientExtractionError` when the
    caller should retry with backoff -- after recording the failure durably.
    """
    session_id = str(session_id)
    limit = max_attempts or globals()["max_attempts"]()
    if candidate_memory.enabled() != "readwrite":
        return {"sessionId": session_id, "status": "SKIPPED", "reason": "memory_disabled"}
    if not evaluation:
        # The report is persisted before dispatch, so this is a session whose
        # evaluation was deleted or never finished -- nothing to extract from.
        return {"sessionId": session_id, "status": "SKIPPED", "reason": "evaluation_not_available"}

    # 1. A cheap look before the lock. Not the authoritative one.
    state = await job_state(db, session_id)
    if _is_done(state):
        trace_event("interviewer", "candidate_memory_job_duplicate", session_id=session_id)
        return {"sessionId": session_id, "status": "SKIPPED", "reason": "already_extracted", "counts": state.get("counts")}
    if _is_permanent_failure(state):
        return {"sessionId": session_id, "status": "FAILED", "permanent": True, "reason": state.get("reason"), "skipped": True}

    if store is None:
        # The store could not be opened: that is a failure to record, never a
        # successful skip. Transient (connection) or permanent (dependency).
        return await _record_failure(
            db, session_id, state,
            store_error or MemoryStoreUnavailable("store_unavailable", permanent=False),
            attempt=attempt, limit=limit,
        )

    user_id = await candidate_memory._user_id_of_session(db, session_id)
    if not user_id:
        return {"sessionId": session_id, "status": "SKIPPED", "reason": "no_user"}

    # 2. The lock. Transaction-scoped: it is released by the commit that
    #    writes DONE (or the rollback on failure), never earlier.
    if not await _try_lock(db, session_id):
        await db.rollback()
        trace_event("interviewer", "candidate_memory_job_busy", session_id=session_id)
        return {"sessionId": session_id, "status": "RETRY", "reason": "in_progress", "retry_in": RETRY_IN_SECONDS}

    # 3. The authoritative look, under the lock. Two deliveries that both
    #    passed step 1 serialize here; the second reads what the first wrote.
    state = await job_state(db, session_id)
    if _is_done(state):
        await db.rollback()
        trace_event("interviewer", "candidate_memory_job_duplicate", session_id=session_id, after_lock=True)
        return {"sessionId": session_id, "status": "SKIPPED", "reason": "already_extracted", "counts": state.get("counts")}

    state = await _write_state(db, session_id, state, status="PROCESSING", attempt=attempt, started_at=_now_iso())
    try:
        counts = await extract_after_session(db, session_id, evaluation=evaluation, store=store, raise_errors=True)
    except Exception as exc:  # noqa: BLE001 - classified and recorded below
        await db.rollback()  # releases the lock
        return await _record_failure(db, session_id, state, exc, attempt=attempt, limit=limit)
    await _write_state(db, session_id, state, status="DONE", attempt=attempt, counts=dict(counts or {}), finished_at=_now_iso())
    await db.commit()  # releases the lock
    return {"sessionId": session_id, "status": "COMPLETE", "counts": dict(counts or {})}


async def _record_failure(
    db: AsyncSession,
    session_id: str,
    state: dict[str, Any] | None,
    exc: BaseException,
    *,
    attempt: int,
    limit: int,
) -> dict[str, Any]:
    """Write FAILED durably; raise for a retry or return a permanent failure."""
    kind = classify(exc)
    permanent = kind == "permanent" or attempt >= limit
    if isinstance(exc, (ImportError, ModuleNotFoundError)) or (isinstance(exc, MemoryStoreUnavailable) and exc.permanent):
        reason = "missing_dependency"
    elif kind == "permanent":
        reason = "rejected"
    elif permanent:
        reason = "attempts_exhausted"
    else:
        reason = "transient"
    error = f"{type(exc).__name__}: {exc}"[:500]
    await _write_state(
        db, session_id, state,
        status="FAILED", attempt=attempt, permanent=permanent, reason=reason, error=error, failed_at=_now_iso(),
    )
    await db.commit()
    trace_event(
        "interviewer", "candidate_memory_job_failed", session_id=session_id,
        attempt=attempt, permanent=permanent, reason=reason, error_type=type(exc).__name__,
    )
    if permanent:
        logger.error("memory extraction for %s failed permanently (%s): %s", session_id, reason, error)
        return {"sessionId": session_id, "status": "FAILED", "permanent": True, "reason": reason, "error": error, "attempt": attempt}
    raise TransientExtractionError(error) from exc


# ---------------------------------------------------------------- dispatch


def enqueue_memory_extraction(session_id: str) -> bool:
    """Send the task; broker errors never invalidate scoring (the caller falls back)."""
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


# ------------------------------------------------------------ reconciliation

_RECONCILE_SQL = """
SELECT e.session_id AS session_id, s.metadata->'memory_extraction' AS job
FROM interview_evaluations e
JOIN interview_sessions s ON s.id = e.session_id
WHERE e.created_at >= now() - make_interval(hours => :hours)
  AND e.created_at <= now() - make_interval(secs => :grace)
  AND COALESCE(s.metadata->'memory_extraction'->>'status', '') <> 'DONE'
  AND COALESCE(s.metadata->'memory_extraction'->>'permanent', 'false') <> 'true'
ORDER BY e.created_at DESC
LIMIT :limit
"""


def _age_seconds(state: dict[str, Any] | None, now: datetime) -> float | None:
    raw = (state or {}).get("updated_at")
    if not raw:
        return None
    try:
        then = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return (now - then).total_seconds()


async def reconcile_memory_extraction(
    db: AsyncSession,
    *,
    limit: int = RECONCILE_LIMIT,
    grace_seconds: int = RECONCILE_GRACE_SECONDS,
    lease_seconds: int = LEASE_SECONDS,
    window_hours: int = RECONCILE_WINDOW_HOURS,
    max_dispatches: int = MAX_DISPATCHES,
    enqueue: Callable[[str], bool] = enqueue_memory_extraction,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Re-dispatch persisted evaluations whose extraction never finished.

    Closes the crash window between the evaluation commit and ``send_task``,
    and recovers from a broker that was down at dispatch time, a worker that
    died mid-run (stale ``PROCESSING``) and a transient failure that ran out
    of Celery retries. Bounded three ways: only evaluations of the last
    ``window_hours`` (the crash window is seconds; a wide window would
    backfill pre-feature sessions at provider cost), at most ``limit``
    sessions per sweep, and ``max_dispatches`` per session, after which the
    job is marked permanently failed so it is not retried forever.
    """
    if candidate_memory.enabled() != "readwrite":
        return {"status": "SKIPPED", "reason": "memory_disabled"}
    now = now or datetime.now(UTC)
    rows = (
        await db.execute(text(_RECONCILE_SQL), {"hours": window_hours, "grace": grace_seconds, "limit": limit})
    ).mappings().all()
    summary = {"status": "COMPLETE", "candidates": len(rows), "dispatched": 0, "fresh": 0, "exhausted": 0, "dispatch_failed": 0}
    for row in rows:
        session_id = str(row["session_id"])
        state = _parse(row["job"])
        age = _age_seconds(state, now)
        if state and state.get("status") in ("QUEUED", "PROCESSING") and age is not None and age < lease_seconds:
            summary["fresh"] += 1
            continue
        dispatches = int((state or {}).get("dispatches") or 0)
        if dispatches >= max_dispatches:
            await _write_state(db, session_id, state, status="FAILED", permanent=True, reason="dispatch_exhausted", dispatches=dispatches)
            summary["exhausted"] += 1
            trace_event("interviewer", "candidate_memory_job_failed", session_id=session_id, permanent=True, reason="dispatch_exhausted")
            continue
        if enqueue(session_id):
            await _write_state(db, session_id, state, status="QUEUED", reason="reconciled", dispatches=dispatches + 1, queued_at=_now_iso())
            summary["dispatched"] += 1
        else:
            summary["dispatch_failed"] += 1
    await db.commit()
    if summary["dispatched"] or summary["exhausted"]:
        trace_event("interviewer", "candidate_memory_reconciled", **{k: v for k, v in summary.items() if k != "status"})
    return summary


async def reconcile_loop(session_factory: Any, *, interval_seconds: float = 300.0) -> None:
    """Run the reconciler forever, every ``interval_seconds``; errors are logged, never fatal."""
    while True:
        try:
            async with session_factory() as db:
                await reconcile_memory_extraction(db)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a sweep must not take the API down
            logger.exception("memory extraction reconcile sweep failed")
        await asyncio.sleep(interval_seconds)
