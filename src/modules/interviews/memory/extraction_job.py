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

**Every transition is one guarded SQL statement.** The new state is merged
into the old one *inside* the UPDATE, and the WHERE clause refuses what must
never happen: nothing overwrites ``DONE``; ``QUEUED`` and ``PROCESSING`` do
not revive a permanent failure; the reconciler's writes are compare-and-set
on ``updated_at`` so a worker that moved first wins. A refused transition
returns ``None`` and the caller re-reads instead of assuming.

What makes the job safe to deliver more than once, to lose a worker on, and
to lose the dispatch of:

* **Idempotency** -- ``DONE`` is read once before the advisory lock (cheap
  exit) and **again after the lock is held**, together with the permanent
  failure check; two deliveries that both passed the first read serialize on
  the lock and the second re-reads what the first wrote. Every failure,
  including a store that cannot be opened, is recorded *under the lock*.
* **Mutual exclusion** -- a transaction-scoped Postgres advisory lock on the
  session id, held for the whole extraction (the transaction that took it is
  the one that writes ``DONE``). A worker that cannot take it asks to be
  retried later rather than failing. The reconciler takes its own sweep lock,
  so concurrent API replicas do not double-dispatch.
* **Worker failure** -- Celery acknowledges late and rejects on worker loss
  (``celery_app.py``); the lock dies with the crashed connection; the state is
  ``PROCESSING`` and the redelivery runs it again.
* **Failure classes** -- disabled memory is a *skip*; a missing dependency
  (the ``memory`` extra) or a request the provider refuses (401/403/400/404)
  is *permanent*; a connection or provider outage is *transient* and retried
  with bounded exponential backoff; an unknown error is treated as transient
  until the attempt cap, then recorded permanent. A transient failure never
  returns a successful result.
* **Dispatch failure** -- ``score`` writes ``QUEUED`` and commits *before*
  ``send_task``. If the broker refuses, it falls back to the pre-§9 inline
  run. If the process dies between the commit and the dispatch, the
  reconciler (:func:`reconcile_memory_extraction`, run periodically by the
  API, or by hand with ``scripts/ops/reconcile_memory_extraction.py`` for a
  window wider than the automatic one) finds evaluations whose job is neither
  ``DONE`` nor permanently failed and dispatches them again, least recently
  attempted first so nothing starves, a bounded number of times.

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
#: for (seen on the dev database: one pre-feature session per sweep). After a
#: longer outage, run scripts/ops/reconcile_memory_extraction.py with an
#: explicit window.
RECONCILE_WINDOW_HOURS = 6
#: Sessions per sweep; with the 5-minute cadence this bounds the dispatch rate.
RECONCILE_LIMIT = 20
#: Dispatches per session before the reconciler gives up for good.
MAX_DISPATCHES = 5
#: Advisory lock key of the sweep itself (one sweep at a time across replicas).
RECONCILE_LOCK_KEY = "interview-memory:reconcile"


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
    """``"transient"`` or ``"permanent"`` for one failure.

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

# One guarded transition. The new fields are merged into the current state
# inside the statement (no read-modify-write), ``dispatches`` is incremented
# in SQL, and the guards below decide whether the write may happen at all.
_TRANSITION = """
UPDATE interview_sessions
SET metadata = jsonb_set(
        COALESCE(metadata, '{{}}'::jsonb), '{{memory_extraction}}',
        COALESCE(metadata->'memory_extraction', '{{}}'::jsonb)
          || CAST(:job AS jsonb)
          || jsonb_build_object(
               'dispatches', COALESCE((metadata->'memory_extraction'->>'dispatches')::int, 0) + :bump),
        true),
    updated_at = now()
WHERE id = :sid{guards}
RETURNING metadata->'memory_extraction'
"""
_GUARD_NOT_DONE = "\n  AND COALESCE(metadata->'memory_extraction'->>'status', '') <> 'DONE'"
_GUARD_NOT_PERMANENT = "\n  AND COALESCE(metadata->'memory_extraction'->>'permanent', 'false') <> 'true'"
_GUARD_SEEN = "\n  AND COALESCE(metadata->'memory_extraction'->>'updated_at', '') = :seen"


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


async def _transition(
    db: AsyncSession,
    session_id: str,
    *,
    bump: int = 0,
    unless_done: bool = True,
    unless_permanent: bool = False,
    seen: str | None = None,
    **fields: Any,
) -> dict[str, Any] | None:
    """Apply one guarded state transition; ``None`` when a guard refused it.

    ``unless_done`` (default) never overwrites a finished job; ``unless_permanent``
    never revives a permanently failed one; ``seen`` makes the write
    compare-and-set on the ``updated_at`` the caller read.
    """
    guards = ""
    if unless_done:
        guards += _GUARD_NOT_DONE
    if unless_permanent:
        guards += _GUARD_NOT_PERMANENT
    params: dict[str, Any] = {
        "sid": str(session_id),
        "bump": int(bump),
        "job": json.dumps({**fields, "updated_at": _now_iso()}, default=str),
    }
    if seen is not None:
        guards += _GUARD_SEEN
        params["seen"] = seen
    return _parse(await db.scalar(text(_TRANSITION.format(guards=guards)), params))


def _is_done(state: dict[str, Any] | None) -> bool:
    return bool(state) and state.get("status") == "DONE"


def _is_permanent_failure(state: dict[str, Any] | None) -> bool:
    return bool(state) and state.get("status") == "FAILED" and bool(state.get("permanent"))


def _settled(session_id: str, state: dict[str, Any], *, after_lock: bool = False) -> dict[str, Any]:
    """The result for a job that is already DONE or permanently FAILED."""
    if _is_done(state):
        trace_event("interviewer", "candidate_memory_job_duplicate", session_id=session_id, after_lock=after_lock)
        return {"sessionId": session_id, "status": "SKIPPED", "reason": "already_extracted", "counts": state.get("counts")}
    return {"sessionId": session_id, "status": "FAILED", "permanent": True, "reason": state.get("reason"), "skipped": True}


async def mark_queued(db: AsyncSession, session_id: str, *, reason: str = "scored") -> dict[str, Any] | None:
    """Record the dispatch durably, and commit, before the task is sent.

    One guarded statement: returns the new state, or ``None`` when the session
    is already ``DONE`` or permanently failed (then nothing should be
    dispatched). Safe under concurrent API replicas: the increment happens in
    SQL and a finished job is never reopened.
    """
    state = await _transition(
        db, session_id, bump=1, unless_done=True, unless_permanent=True,
        status="QUEUED", reason=reason, queued_at=_now_iso(),
    )
    await db.commit()
    return state


async def _try_lock(db: AsyncSession, key: str) -> bool:
    """A transaction-scoped advisory lock; released at commit/rollback."""
    locked = await db.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"), {"key": key})
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
    if _is_done(state) or _is_permanent_failure(state):
        return _settled(session_id, state)

    user_id = await candidate_memory._user_id_of_session(db, session_id)
    if not user_id:
        return {"sessionId": session_id, "status": "SKIPPED", "reason": "no_user"}

    # 2. The lock. Transaction-scoped: it is released by the commit that
    #    writes DONE or FAILED (or the rollback on an extraction error), never
    #    earlier. Every state transition below happens while it is held.
    if not await _try_lock(db, f"interview-memory:{session_id}"):
        await db.rollback()
        trace_event("interviewer", "candidate_memory_job_busy", session_id=session_id)
        return {"sessionId": session_id, "status": "RETRY", "reason": "in_progress", "retry_in": RETRY_IN_SECONDS}

    # 3. The authoritative look, under the lock. Two deliveries that both
    #    passed step 1 serialize here; the second reads what the first wrote,
    #    DONE or permanently FAILED alike.
    state = await job_state(db, session_id)
    if _is_done(state) or _is_permanent_failure(state):
        await db.rollback()
        return _settled(session_id, state, after_lock=True)

    if store is None:
        # The store could not be opened: a failure to record -- under the lock,
        # like every other one -- never a successful skip.
        return await _record_failure(
            db, session_id, store_error or MemoryStoreUnavailable("store_unavailable", permanent=False),
            attempt=attempt, limit=limit,
        )

    await _transition(db, session_id, status="PROCESSING", attempt=attempt, started_at=_now_iso())
    try:
        counts = await extract_after_session(db, session_id, evaluation=evaluation, store=store, raise_errors=True)
    except Exception as exc:  # noqa: BLE001 - classified and recorded below
        # The failing transaction is rolled back (the lock goes with it); the
        # failure is then recorded by a fresh, guarded transition -- never from
        # a state read before the extraction started.
        await db.rollback()
        return await _record_failure(db, session_id, exc, attempt=attempt, limit=limit)
    done = await _transition(
        db, session_id, status="DONE", attempt=attempt, counts=dict(counts or {}), finished_at=_now_iso()
    )
    await db.commit()  # releases the lock
    if done is None:  # cannot happen under the lock; if it ever does, say so rather than lie
        logger.error("memory extraction for %s finished but its DONE transition was refused", session_id)
    return {"sessionId": session_id, "status": "COMPLETE", "counts": dict(counts or {})}


async def _record_failure(
    db: AsyncSession,
    session_id: str,
    exc: BaseException,
    *,
    attempt: int,
    limit: int,
) -> dict[str, Any]:
    """Write FAILED durably (never over DONE); raise for a retry or return a permanent failure."""
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
    state = await _transition(
        db, session_id, status="FAILED", attempt=attempt, permanent=permanent, reason=reason, error=error,
        failed_at=_now_iso(),
    )
    await db.commit()
    if state is None:
        # Another delivery finished the job while this one was failing: its
        # DONE stands, and this failure is not recorded over it.
        current = await job_state(db, session_id)
        if _is_done(current):
            return _settled(session_id, current, after_lock=True)
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

# Eligible: a persisted evaluation inside the window, past the grace period,
# whose job is neither DONE nor permanently FAILED. Least recently attempted
# first (a job never attempted counts from its evaluation), so a backlog
# wider than ``limit`` rotates instead of starving its oldest members.
_RECONCILE_SQL = """
SELECT e.session_id AS session_id, s.metadata->'memory_extraction' AS job
FROM interview_evaluations e
JOIN interview_sessions s ON s.id = e.session_id
WHERE e.created_at >= now() - make_interval(hours => :hours)
  AND e.created_at <= now() - make_interval(secs => :grace)
  AND COALESCE(s.metadata->'memory_extraction'->>'status', '') <> 'DONE'
  AND COALESCE(s.metadata->'memory_extraction'->>'permanent', 'false') <> 'true'
ORDER BY COALESCE((s.metadata->'memory_extraction'->>'updated_at')::timestamptz, e.created_at) ASC
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
    dry_run: bool = False,
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

    Concurrency: one sweep at a time (an advisory lock on the sweep; a replica
    that cannot take it skips this round), and every write is compare-and-set
    on the ``updated_at`` the sweep read, so a worker that moved first wins.

    ``dry_run`` reads and reports what the sweep *would* do; it takes no lock,
    writes nothing and dispatches nothing.
    """
    if candidate_memory.enabled() != "readwrite":
        return {"status": "SKIPPED", "reason": "memory_disabled"}
    if not dry_run and not await _try_lock(db, RECONCILE_LOCK_KEY):
        await db.rollback()
        return {"status": "SKIPPED", "reason": "sweep_in_progress"}
    now = now or datetime.now(UTC)
    rows = (
        await db.execute(text(_RECONCILE_SQL), {"hours": window_hours, "grace": grace_seconds, "limit": limit})
    ).mappings().all()
    summary: dict[str, Any] = {
        "status": "DRY_RUN" if dry_run else "COMPLETE",
        "candidates": len(rows), "dispatched": 0, "fresh": 0, "exhausted": 0, "dispatch_failed": 0, "raced": 0,
    }
    planned: list[dict[str, Any]] = []
    for row in rows:
        session_id = str(row["session_id"])
        state = _parse(row["job"])
        seen = str((state or {}).get("updated_at") or "")
        age = _age_seconds(state, now)
        if state and state.get("status") in ("QUEUED", "PROCESSING") and age is not None and age < lease_seconds:
            summary["fresh"] += 1
            continue
        dispatches = int((state or {}).get("dispatches") or 0)
        if dispatches >= max_dispatches:
            planned.append({"session_id": session_id, "action": "exhaust", "dispatches": dispatches})
            if dry_run:
                summary["exhausted"] += 1
                continue
            written = await _transition(
                db, session_id, seen=seen, status="FAILED", permanent=True, reason="dispatch_exhausted",
            )
            if written is None:
                summary["raced"] += 1
                continue
            summary["exhausted"] += 1
            trace_event("interviewer", "candidate_memory_job_failed", session_id=session_id, permanent=True, reason="dispatch_exhausted")
            continue
        planned.append({"session_id": session_id, "action": "dispatch", "dispatches": dispatches + 1})
        if dry_run:
            summary["dispatched"] += 1
            continue
        if not enqueue(session_id):
            summary["dispatch_failed"] += 1
            continue
        written = await _transition(
            db, session_id, bump=1, unless_permanent=True, seen=seen,
            status="QUEUED", reason="reconciled", queued_at=_now_iso(),
        )
        if written is None:
            # The job moved under us (a worker finished or failed it for good)
            # between the read and this write; the dispatch just sent will find
            # that state and stop. Nothing of ours is written over it.
            summary["raced"] += 1
            continue
        summary["dispatched"] += 1
    if dry_run:
        summary["planned"] = planned
        return summary
    await db.commit()  # releases the sweep lock
    if summary["dispatched"] or summary["exhausted"] or summary["raced"]:
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
