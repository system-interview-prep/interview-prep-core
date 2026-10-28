"""Persistent progress of one CV upload / parse attempt, for the admin CV-parsing view.

The upload service opens a context with ``cv_run`` around its upload events,
and every ``CvParsingPipeline.run`` call opens one per parse attempt (the
Celery task opens it first so the attempt number and parser mode are known).
While it is open, the events emitted here and by the parsing pipeline /
document extractor are stored in ``cv_pipeline_events``
(src/core/pipeline_events.py) and every model call carries the CV id.

Fields are small scalars, counts and codes only: never CV text, file names,
identity data, evidence quotes or exception messages (they may quote the CV).
Every helper swallows its own errors, so tracking can never break an upload or
a parse.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import PurePath
from typing import Any

from src.core.pipeline_events import CvRun, current_cv_run
from src.core.trace_logging import trace_event

logger = logging.getLogger(__name__)

UPLOAD_WORKFLOW = "cv_upload"
PARSER_WORKFLOW = "cv_parser"
_MAX_WARNINGS = 40
_MAX_CODE = 64


def active_run() -> CvRun | None:
    return current_cv_run.get()


@contextmanager
def cv_run(
    cv_id: str,
    *,
    user_id: str | None = None,
    attempt_id: str | None = None,
    attempt: int | None = None,
    parser_mode: str | None = None,
    reuse_active: bool = False,
) -> Iterator[CvRun | None]:
    """Open a CV context for the block; yields None when it cannot be opened.

    ``reuse_active`` keeps an already open context of the same CV (the
    Celery task opens one before the pipeline does).
    """
    token = None
    run: CvRun | None = None
    try:
        existing = current_cv_run.get()
        if reuse_active and existing is not None and existing.cv_id == str(cv_id)[:36]:
            run = existing
        else:
            run = CvRun(
                cv_id=str(cv_id)[:36],
                attempt_id=(attempt_id or str(uuid.uuid4()))[:36],
                user_id=str(user_id)[:36] if user_id else None,
                attempt=attempt,
                parser_mode=parser_mode,
            )
            token = current_cv_run.set(run)
    except Exception:  # noqa: BLE001 - tracking must never break CV parsing
        logger.debug("cv run not opened", exc_info=True)
    try:
        yield run
    finally:
        if token is not None:
            try:
                current_cv_run.reset(token)
            except Exception:  # noqa: BLE001
                current_cv_run.set(None)


def emit(event: str, *, workflow: str = PARSER_WORKFLOW, **fields: Any) -> None:
    """Emit one CV event (also written to the dev JSONL trace); never raises."""
    try:
        trace_event(workflow, event, **fields)
    except Exception:  # noqa: BLE001 - tracking must never break CV parsing
        logger.debug("cv run event failed", exc_info=True)


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def monotonic_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


# --------------------------------------------------------------------------- field builders


def file_extension(filename: str | None) -> str | None:
    """``pdf`` for ``Nguyen Van A CV.pdf``: the name itself may identify the candidate."""
    try:
        suffix = PurePath(str(filename or "")).suffix.lower().lstrip(".")
    except Exception:  # noqa: BLE001
        return None
    return suffix[:10] or None


def _code(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:_MAX_CODE] or None


def warning_section(path: Any) -> str | None:
    """``employment`` for ``employment[0].startDate``; only the leading identifier."""
    if not path:
        return None
    head = str(path).split(".", 1)[0].split("[", 1)[0].split("/", 1)[0].strip()
    if not head or not head.replace("_", "").isalnum():
        return None
    return head[:40]


def warning_items(warnings: Any) -> list[dict[str, Any]]:
    """``[{code, severity, section}]`` of parser warnings, capped; never their message."""
    items: list[dict[str, Any]] = []
    try:
        for warning in list(warnings or [])[:_MAX_WARNINGS]:
            get = warning.get if isinstance(warning, dict) else (lambda k, w=warning: getattr(w, k, None))
            items.append(
                {
                    "code": _code(get("code")) or "unknown",
                    "severity": _code(get("severity")),
                    "section": warning_section(get("path")),
                }
            )
    except Exception:  # noqa: BLE001
        logger.debug("cv warnings not summarised", exc_info=True)
    return items


def resume_counts(resume: Any) -> dict[str, int]:
    """Entry counts of a canonical resume (no content)."""
    counts: dict[str, int] = {}
    for key in (
        "skills",
        "employment",
        "education",
        "projects",
        "certifications",
        "languages",
        "career_classifications",
        "evidence",
    ):
        try:
            counts[key] = len(getattr(resume, key, None) or [])
        except Exception:  # noqa: BLE001
            counts[key] = 0
    return counts


def parser_completed_fields(parsed: Any, *, duration_ms: int, llm_calls_before: int | None) -> dict[str, Any]:
    resume = getattr(parsed, "resume", None)
    parsing = getattr(resume, "parsing", None)
    run = active_run()
    counts = resume_counts(resume)
    fields: dict[str, Any] = {
        "parser_version": getattr(parsing, "parser_version", None) or "unknown",
        "parser_mode": run.parser_mode if run else None,
        "parsing_status": getattr(parsing, "status", None),
        **{f"{key}_count": value for key, value in counts.items() if key != "evidence"},
        "evidence_count": counts.get("evidence", 0),
        "warnings_count": len(getattr(parsing, "warnings", None) or []),
        "duration_ms": duration_ms,
    }
    if run is not None and llm_calls_before is not None:
        fields["llm_calls"] = max(0, run.ai_calls - llm_calls_before)
        fields["llm_used"] = fields["llm_calls"] > 0
    return fields


def ai_calls_so_far() -> int | None:
    run = active_run()
    return run.ai_calls if run is not None else None


_PHASE_REASONS = {
    "storage": "storage_read_failed",
    "extract": "extraction_failed",
    "source": "source_build_failed",
    "parse": "parser_failed",
    "quality": "quality_gate_failed",
    "save": "save_failed",
}


def failure_reason(exc: BaseException, phase: str | None) -> str:
    """A short, fixed reason code for a failed attempt; never the exception text."""
    try:
        if isinstance(exc, TimeoutError):
            return "timeout"
        name = type(exc).__name__
        if name == "AllExtractorsFailedError":
            return "all_extractors_failed"
        if name == "EmptyExtractionError":
            return "empty_extraction"
        if name == "ValidationError":
            return "invalid_parser_output"
        module = type(exc).__module__ or ""
        if module.startswith(("sqlalchemy", "asyncpg")):
            return "database_error"
        if module.startswith(("botocore", "boto3")):
            return "storage_error"
        return _PHASE_REASONS.get(phase or "", "unexpected_error")
    except Exception:  # noqa: BLE001
        return "unexpected_error"
