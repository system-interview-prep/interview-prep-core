"""Persistent progress of one CV-JD matching run, for the admin matching view.

Each matching entry point (the /match and /match-ids endpoints, the
clarification endpoints, the interview planner, the Celery task) opens a run
with ``match_run``. While it is open, the run-level events emitted here are
stored in ``matching_pipeline_events`` (src/core/pipeline_events.py) and every
model call carries the run id.

Fields are small scalars, counts and codes only: never CV text, evidence
quotes, candidate answers, generated questions or personal data beyond ids.
Every helper swallows its own errors, so tracking can never break matching.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from src.core.pipeline_events import MatchRun, current_match_run
from src.core.trace_logging import trace_event

logger = logging.getLogger(__name__)

FLOWS = ("match", "clarify_prepare", "clarify_questions", "clarify_full", "rescore", "planner", "celery")
_MAX_ITEMS = 60
_MAX_LABEL = 80
# Key under which the run travels inside a Celery task payload.
CELERY_RUN_KEY = "_matchRun"


def active_run() -> MatchRun | None:
    return current_match_run.get()


@contextmanager
def match_run(
    flow: str,
    *,
    run_id: str | None = None,
    user_id: str | None = None,
    resume_id: str | None = None,
    job_id: str | None = None,
    reuse_active: bool = False,
) -> Iterator[MatchRun]:
    """Open a run for the duration of the block (contextvars reach threadpool calls)."""
    existing = current_match_run.get()
    if reuse_active and existing is not None:
        yield existing
        return
    run = MatchRun(
        run_id=(run_id or str(uuid.uuid4()))[:36],
        flow=flow,
        user_id=str(user_id) if user_id else None,
        resume_id=str(resume_id) if resume_id else None,
        job_id=str(job_id) if job_id else None,
    )
    token = current_match_run.set(run)
    try:
        yield run
    finally:
        current_match_run.reset(token)


def emit(event: str, *, workflow: str = "matching", **fields: Any) -> None:
    """Emit one run event (also written to the dev JSONL trace)."""
    try:
        trace_event(workflow, event, **fields)
    except Exception:  # noqa: BLE001 - tracking must never break matching
        logger.debug("matching run event failed", exc_info=True)


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def celery_run_payload(run: MatchRun) -> dict[str, Any]:
    return {"runId": run.run_id, "userId": run.user_id, "resumeId": run.resume_id, "jobId": run.job_id}


# --------------------------------------------------------------------------- field builders


def _round(value: Any, digits: int = 4) -> float | None:
    try:
        return round(float(value), digits) if value is not None else None
    except (TypeError, ValueError):
        return None


def _label(requirement: Any) -> str:
    """JD requirement label (JD text, not candidate data), truncated."""
    label = (
        getattr(requirement, "raw_label", None)
        or getattr(getattr(requirement, "skill", None), "label", None)
        or (
            f"language {requirement.language_code}"
            if getattr(requirement, "language_code", None)
            else None
        )
        or getattr(requirement, "requirement_id", "")
    )
    label = " ".join(str(label).split())
    return label if len(label) <= _MAX_LABEL else label[: _MAX_LABEL - 1] + "…"


def requirement_evaluator(requirement: Any) -> str:
    from src.modules.matching.domain.schemas import (
        LanguageRequirement,
        SkillRequirement,
        UnresolvedRequirement,
    )
    from src.modules.matching.evaluation.requirement_evaluators import select_evaluator

    if isinstance(requirement, SkillRequirement):
        return "knowledge_skill"
    if isinstance(requirement, LanguageRequirement):
        return "language"
    if isinstance(requirement, UnresolvedRequirement):
        try:
            return select_evaluator(requirement).name
        except Exception:  # noqa: BLE001
            return "generic_requirement"
    return "unknown"


def inputs_fields(resume: Any, job: Any) -> dict[str, Any]:
    """``inputs_resolved`` fields for a resolved CV and JD."""
    parsing = getattr(resume, "parsing", None)
    requirements = list(getattr(job, "requirements", []) or [])
    legacy = str(getattr(job, "document_id", "")) == f"doc-jd-{getattr(job, 'job_id', '')}"
    return {
        "jd_path": "legacy" if legacy else "canonical",
        "requirement_count": len(requirements),
        "requirements_by_priority": dict(Counter(str(r.priority) for r in requirements)),
        "cv_parser_status": parsing.status if parsing else "legacy",
        "has_raw_text": bool(getattr(resume, "raw_text", None)),
        "cv_evidence_count": len(getattr(resume, "evidence", []) or []),
    }


def requirements_fields(
    requirements: list[Any], results: list[Any], overridden_ids: set[str] | None = None
) -> dict[str, Any]:
    """``requirements_evaluated`` fields: counts plus a capped per-requirement list."""
    overridden_ids = overridden_ids or set()
    by_status: Counter[str] = Counter()
    by_evaluator: Counter[str] = Counter()
    must_unknown = must_not_met = 0
    items = []
    for requirement, result in zip(requirements, results, strict=False):
        evaluator = (
            "override" if result.requirement_id in overridden_ids else requirement_evaluator(requirement)
        )
        by_status[result.status] += 1
        by_evaluator[evaluator] += 1
        if requirement.priority == "must_have":
            must_unknown += result.status == "unknown"
            must_not_met += result.status == "not_met"
        if len(items) < _MAX_ITEMS:
            items.append(
                {
                    "requirement_id": result.requirement_id,
                    "label": _label(requirement),
                    "priority": requirement.priority,
                    "status": result.status,
                    "evaluator": evaluator,
                    "reason_code": result.reason_code,
                    "confidence": _round(result.confidence, 3),
                }
            )
    return {
        "total": len(results),
        "counts_by_status": dict(by_status),
        "counts_by_evaluator": dict(by_evaluator),
        "must_have_unknown": must_unknown,
        "must_have_not_met": must_not_met,
        "overrides_applied": len(overridden_ids),
        "items": items,
    }


def scoring_fields(result: Any) -> dict[str, Any]:
    """``match_scored`` fields from a MatchResult (tolerates partial objects)."""
    provenance = getattr(result, "score_provenance", None)
    factors = {}
    for factor in getattr(result, "factor_results", None) or []:
        factors[str(factor.factor)] = {
            "score": _round(factor.raw_score),
            "weight": _round(factor.effective_weight),
        }
    return {
        "eligibility": getattr(result, "eligibility", None),
        "compatibility_status": getattr(result, "compatibility_status", None),
        "suitability_score": _round(getattr(result, "suitability_score", None)),
        "diagnostic_score": _round(getattr(result, "diagnostic_score", None)),
        "fit_band": getattr(result, "fit_band", None),
        "decision": getattr(result, "decision", None),
        "provenance_mode": getattr(provenance, "mode", None),
        "scored_count": getattr(provenance, "scored_requirement_count", None),
        "unknown_count": getattr(provenance, "unknown_requirement_count", None),
        "total_count": getattr(provenance, "total_requirement_count", None),
        "failed_must_have_count": len(getattr(result, "failed_must_have_requirements", None) or []),
        "factors": factors,
        "warnings": [str(w) for w in (getattr(result, "warnings", None) or [])],
    }


def emit_match_scored(result: Any) -> None:
    if active_run() is None:
        return
    try:
        emit("match_scored", **scoring_fields(result))
    except Exception:  # noqa: BLE001 - tracking must never break matching
        logger.debug("match_scored not recorded", exc_info=True)


def emit_match_completed(
    result: Any, started: float | None = None, *, workflow: str = "matching"
) -> None:
    """``match_completed``; on a cache hit it also carries the cached scoring summary.

    ``started`` defaults to when the run opened.
    """
    run = active_run()
    if run is None:
        return
    try:
        fields: dict[str, Any] = {
            "duration_ms": elapsed_ms(run.started if started is None else started),
            "decision": getattr(result, "decision", None),
            "fit_band": getattr(result, "fit_band", None),
            "suitability_score": _round(getattr(result, "suitability_score", None)),
            "cache_hit": run.cache_hit,
        }
        if run.cache_hit:
            fields["scoring"] = scoring_fields(result)
        emit("match_completed", workflow=workflow, **fields)
    except Exception:  # noqa: BLE001
        logger.debug("match_completed not recorded", exc_info=True)


def emit_match_failed(
    exc: BaseException | None,
    started: float | None = None,
    *,
    workflow: str = "matching",
    error_type: str | None = None,
    **extra: Any,
) -> None:
    run = active_run()
    if run is None:
        return
    emit(
        "match_failed",
        workflow=workflow,
        duration_ms=elapsed_ms(run.started if started is None else started),
        error_type=error_type or (type(exc).__name__ if exc is not None else "unknown"),
        http_status=getattr(exc, "status_code", None),
        cache_hit=run.cache_hit,
        **extra,
    )


_HTTP_REASONS = {400: "not_parsed", 403: "forbidden", 404: "not_found", 422: "invalid", 500: "error"}


def input_failure_reason(which: str, exc: BaseException) -> tuple[int | None, str]:
    """(http_status, reason code) for a failed CV/JD lookup; never the error text."""
    status = getattr(exc, "status_code", None)
    return status, f"{which}_{_HTTP_REASONS.get(status, 'error')}"


def request_fields(endpoint: str, policy: Any, *, is_async: bool) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        "policy_version": getattr(policy, "policy_version", None),
        "semantic_mode": getattr(policy, "semantic_mode", None),
        "bm25_provider_mode": getattr(policy, "bm25_provider_mode", None),
        "async": bool(is_async),
    }
