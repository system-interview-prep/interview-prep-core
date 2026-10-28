"""Persisted matching results, so the same CV and JD always get the same match.

Matching is recomputed for every new interview session and every matching
page view. It is mostly deterministic, but targeted re-parsing calls an LLM
when CV evidence is thin, so the agenda (targets, gap stages, the project
question) could change between two sessions on the same CV and JD. The result
is cached by a hash of the full request: any edit to the parsed CV, the JD
version, the policy or the candidate preferences produces a new key.

Only callers on the API event loop use this (it uses the pooled SessionFactory).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy import text

from src.infrastructure.database import SessionFactory
from src.modules.matching.application import run_tracking
from src.modules.matching.application.targeted_reparse import evidence_search_failed
from src.modules.matching.domain.schemas import MatchRequest, MatchResult

logger = logging.getLogger(__name__)

# Bump when matching logic changes in a way that should invalidate stored results.
CACHE_VERSION = "1"
# Taxonomy aliases and embeddings live outside the request; refresh eventually.
MAX_AGE = timedelta(days=30)


_PIPELINE_VERSION = MatchResult.model_fields["pipeline_version"].default


def match_cache_key(payload: MatchRequest) -> str:
    body = payload.model_dump_json(by_alias=True, exclude={"async_processing"})
    material = f"{CACHE_VERSION}\x00{_PIPELINE_VERSION}\x00{body}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def cached_match(
    payload: MatchRequest, compute: Callable[[MatchRequest], Awaitable[MatchResult]]
) -> MatchResult:
    """Return the stored result for this request, computing and storing it once."""
    key = match_cache_key(payload)
    run = run_tracking.active_run()
    if run is not None:
        run.cache_key = key
    started = time.perf_counter()
    try:
        age_s = None
        async with SessionFactory() as db:
            stored = await db.scalar(
                text(
                    "SELECT result FROM matching_result_cache "
                    "WHERE cache_key = :key AND created_at > now() - make_interval(days => :days)"
                ),
                {"key": key, "days": MAX_AGE.days},
            )
            if stored is not None and run is not None:
                age_s = await _age_seconds(db, key)
        cached = None
        if isinstance(stored, str):
            cached = MatchResult.model_validate_json(stored)
        elif stored is not None:
            cached = MatchResult.model_validate(stored)
        _trace_lookup(run, hit=cached is not None, age_s=age_s, started=started)
        if cached is not None:
            return cached
    except Exception as exc:
        # The cache only adds stability; matching must work without it.
        logger.warning("Matching cache read failed", exc_info=True)
        _trace_lookup(run, hit=False, age_s=None, started=started, error=exc)

    # Round-trip through JSON so the first call returns exactly what later
    # calls read back from the cache.
    failed_token = evidence_search_failed.set(False)
    try:
        result = MatchResult.model_validate_json((await compute(payload)).model_dump_json(by_alias=True))
        degraded = evidence_search_failed.get()
    finally:
        evidence_search_failed.reset(failed_token)
    if degraded:
        # A flaky provider would otherwise pin a worse match for MAX_AGE.
        logger.warning("Matching result not cached: LLM evidence search failed")
        _trace_write(run, "skipped_degraded")
        return result
    try:
        async with SessionFactory() as db:
            await db.execute(
                text(
                    """
                    INSERT INTO matching_result_cache (cache_key, resume_id, job_id, result, created_at)
                    VALUES (:key, :resume_id, :job_id, CAST(:result AS json), now())
                    ON CONFLICT (cache_key) DO UPDATE
                    SET result = EXCLUDED.result, created_at = EXCLUDED.created_at
                    """
                ),
                {
                    "key": key,
                    "resume_id": payload.resume.resume_id,
                    "job_id": payload.job.job_id,
                    "result": result.model_dump_json(by_alias=True),
                },
            )
            await db.commit()
        _trace_write(run, "stored")
    except Exception as exc:
        logger.warning("Matching cache write failed", exc_info=True)
        _trace_write(run, "write_failed", error_type=type(exc).__name__)
    return result


async def _age_seconds(db, key: str) -> int | None:
    try:
        value = await db.scalar(
            text(
                "SELECT EXTRACT(EPOCH FROM now() - created_at) FROM matching_result_cache "
                "WHERE cache_key = :key"
            ),
            {"key": key},
        )
        return int(float(value)) if value is not None else None
    except Exception:  # noqa: BLE001 - the age is informative only
        return None


def _trace_lookup(
    run, *, hit: bool, age_s: int | None, started: float, error: Exception | None = None
) -> None:
    if run is None:
        return
    run.cache_hit = hit
    run_tracking.emit(
        "cache_lookup_completed",
        event_status="error" if error is not None else "info",
        hit=hit,
        age_s=age_s,
        duration_ms=run_tracking.elapsed_ms(started),
        error_type=type(error).__name__ if error is not None else None,
    )


def _trace_write(run, outcome: str, **fields) -> None:
    if run is None:
        return
    # A failed write is a warning: the match itself succeeded.
    status = "warning" if outcome == "write_failed" else "skipped" if outcome != "stored" else "completed"
    run_tracking.emit("cache_write_completed", event_status=status, outcome=outcome, **fields)
