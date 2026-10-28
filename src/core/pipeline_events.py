"""Persisted pipeline events and AI call logs for the admin views.

``trace_event`` writes local JSONL files in development only. Admins need the
same story in every environment: what the AI did for one interview (entry
check, plan, question selection, each turn, grading) and every model call it
made. This module keeps that record in two tables:

* ``interview_pipeline_events`` - one row per interview pipeline event that
  carries a session id (stage, event, status, duration, small JSON payload);
* ``ai_call_logs`` - one row per model call (purpose, model, latency, tokens,
  outcome), with or without a session.

Recording never blocks or fails the caller. Records go to an in-memory buffer
that a daemon thread flushes every second with its own event loop and an
unpooled engine, so it works the same in the API process, a threadpool thread
or a Celery worker. When the database is unreachable the batch is dropped and
logged; the buffer is bounded so memory cannot grow without limit.

Live updates: after each successful write the flusher hands a short summary
per session to the listeners registered in this process (the API's socket
bridge, src/modules/interviews/application/pipeline_live.py) and sends the same
summary with ``pg_notify`` so other processes (the grading worker) reach the
admin pages too. The bridge skips notifications from its own process.

Privacy: candidate answers are already stored in ``interview_chat_messages``,
so the candidate's words are never copied into events; long strings are
truncated; model inputs and outputs are stored only when
``AI_CALL_LOG_CAPTURE_TEXT`` is on. Rows older than the retention window are
deleted by the flusher.

Matching runs: events of the ``matching`` and ``clarification`` workflows are
stored in ``matching_pipeline_events`` while a match run is active
(``current_match_run``, set at each matching entry point). Only the named
run-level events in ``MATCHING_EVENT_STAGES`` are kept (never one row per
requirement), payloads lose every text-bearing key (CV text, quotes, answers,
questions), and model calls made during a run carry its ``match_run_id``.
They are written in their own transaction, so a missing table (migration
0032 not applied) never drops interview events.

CV parsing: events of the ``cv_upload``, ``cv_parser`` and
``document_extractor`` workflows are stored in ``cv_pipeline_events`` while a
CV context is active (``current_cv_run``, set by the upload service and by
each parse attempt, src/modules/user_cvs/application/run_tracking.py). Only the
events of ``CV_EVENT_STAGES`` are kept, payloads lose every text-bearing key
(including exception messages, which may quote the CV), and model calls made
while parsing carry ``ai_call_logs.cv_id``. Own transaction too (migration 0033).
"""

from __future__ import annotations

import asyncio
import atexit
import contextvars
import json
import logging
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# The interview a request is working on. Set the first time an event of the
# request names a session, so later events of the same request (the engine's
# guardrail events, question generation, model calls) are attached to it.
current_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "pipeline_session_id", default=None
)



@dataclass
class MatchRun:
    """One CV-JD matching run (an HTTP request, a planner match or a Celery task).

    Mutable on purpose: the cache fills ``cache_key``/``cache_hit`` after the
    run started, and threads started from the request see the same object.
    """

    run_id: str
    flow: str
    user_id: str | None = None
    resume_id: str | None = None
    job_id: str | None = None
    cache_key: str | None = None
    cache_hit: bool | None = None
    # perf_counter() when the run opened, for the run's total duration.
    started: float = field(default_factory=time.perf_counter)


current_match_run: contextvars.ContextVar[MatchRun | None] = contextvars.ContextVar(
    "pipeline_match_run", default=None
)


@dataclass
class CvRun:
    """One CV upload or one parse attempt (``attempt_id``) of one CV.

    Mutable: the worker fills ``user_id`` once the CV is claimed, and model
    calls made during the attempt are counted for ``parser_completed``.
    """

    cv_id: str
    attempt_id: str
    user_id: str | None = None
    # Celery attempt number (retries + 1) and configured parser mode, when known.
    attempt: int | None = None
    parser_mode: str | None = None
    ai_calls: int = 0
    ai_errors: int = 0
    started: float = field(default_factory=time.perf_counter)


current_cv_run: contextvars.ContextVar[CvRun | None] = contextvars.ContextVar(
    "pipeline_cv_run", default=None
)

PERSISTED_WORKFLOWS = frozenset({"interviewer", "interview", "naturalness", "voice"})
MATCHING_WORKFLOWS = frozenset({"matching", "clarification"})
# The only matching events that are persisted, with their stage.
MATCHING_EVENT_STAGES: dict[str, str] = {
    "match_requested": "request",
    "match_enqueued": "request",
    "inputs_resolved": "inputs",
    "inputs_failed": "inputs",
    "cache_lookup_completed": "cache",
    "targeted_reparse_completed": "reparse",
    "targeted_reparse_skipped": "reparse",
    "requirements_evaluated": "requirements",
    "semantic_scored": "semantic",
    "match_scored": "scoring",
    "cache_write_completed": "cache_write",
    "match_completed": "done",
    "match_failed": "done",
    "clarification_gate_completed": "clarify",
    "clarification_questions_completed": "questions",
    "rescore_completed": "rescore",
}
CV_WORKFLOWS = frozenset({"cv_upload", "cv_parser", "document_extractor"})
# The only CV events that are persisted, with their stage.
CV_EVENT_STAGES: dict[str, str] = {
    "cv_uploaded": "upload",
    "cv_upload_duplicate": "upload",
    "cv_dispatch_failed": "upload",
    "cv_enqueued": "queue",
    "cv_parse_claimed": "queue",
    "not_claimed": "queue",
    "extraction_started": "extract",
    "provider_failed": "extract",
    "provider_skipped": "extract",
    "provider_fallback_used": "extract",
    "extraction_completed": "extract",
    "source_built": "source",
    "parser_started": "parse",
    "parser_completed": "parse",
    "quality_gate": "quality",
    "cv_saved": "save",
    "completed": "done",
    "failed": "done",
    "retry_scheduled": "done",
}
# Status of each CV event (quality_gate depends on its gate status).
CV_EVENT_STATUSES: dict[str, str] = {
    "cv_uploaded": "completed",
    "cv_upload_duplicate": "info",
    "cv_dispatch_failed": "error",
    "cv_enqueued": "info",
    "cv_parse_claimed": "started",
    "not_claimed": "skipped",
    "extraction_started": "started",
    "provider_failed": "warning",
    "provider_skipped": "info",
    "provider_fallback_used": "warning",
    "extraction_completed": "completed",
    "source_built": "completed",
    "parser_started": "started",
    "parser_completed": "completed",
    "cv_saved": "completed",
    "completed": "completed",
    "failed": "error",
    "retry_scheduled": "warning",
}
_QUALITY_OK = frozenset({"ready", "completed", "ok"})
# The requirements event lists up to 60 requirements.
_MATCHING_MAX_LIST = 60
MATCHING_STATUSES = frozenset({"started", "completed", "error", "warning", "info", "skipped"})
# Columns of the row, or text that may carry CV content, quotes, candidate
# answers, generated questions or PII. Dropped at every nesting level.
_MATCHING_DROPPED_FIELDS = frozenset(
    {
        "event_status",
        "run_id",
        "flow",
        "cache_key",
        "user_id",
        "resume_id",
        "job_id",
        "session_id",
        "candidate",
        "candidate_text",
        "answer",
        "answers",
        "answer_text",
        "raw_text",
        "cv_text",
        "text",
        "texts",
        "quote",
        "exact_quote",
        "evidence_text",
        "evidence_texts",
        "excerpt",
        "snippet",
        "content",
        "question",
        "question_text",
        "prompt",
        "input_text",
        "output_text",
        "requirement_text",
        "name",
        "full_name",
        "email",
        "phone",
        "detail",
    }
)
# CV events: also the row columns, file names, storage keys and every
# free-text error or warning message (an exception or a parser warning may
# quote the CV).
_CV_DROPPED_FIELDS = _MATCHING_DROPPED_FIELDS | frozenset(
    {
        "cv_id",
        "document_id",
        "attempt_id",
        "error",
        "errors",
        "error_message",
        "message",
        "messages",
        "reason_text",
        "filename",
        "file_name",
        "original_name",
        "storage_key",
        "artifact_key",
        "source_artifact_key",
        "url",
        "checksum",
        "document_sha256",
        "path",
        "markdown",
        "identity",
        "emails",
        "phones",
        "headline",
        "summary",
    }
)
# The candidate's words live in interview_chat_messages; never copy them here.
_DROPPED_FIELDS = frozenset({"candidate", "user_id", "candidate_text", "answer_text"})
_MAX_STRING = 600
_MAX_LIST = 40
_BUFFER_LIMIT = 5000
_FLUSH_INTERVAL_SECONDS = 1.0
# Wait this long after the first event of a burst so it lands in one insert;
# short, because live admin pages refresh when the rows are written.
_DEBOUNCE_SECONDS = 0.25
_RETENTION_CHECK_SECONDS = 3600.0

# Identifies this process in pg_notify payloads, so the API does not relay
# its own writes twice (it already got them from the local listener).
INSTANCE_ID = uuid.uuid4().hex
NOTIFY_CHANNEL = "pipeline_events"
_NOTIFY_MAX_BYTES = 7000  # Postgres limit is 8000 bytes per payload
_SUMMARY_EVENTS = 20

# Voice-room incidents worth a colored row (the candidate was not heard, a
# provider failed over, the candidate left the room).
_WARNING_EVENTS = frozenset(
    {
        "voice_not_heard",
        "voice_unclear_transcript",
        "voice_tts_fallback",
        "voice_provider_degraded",
        "voice_candidate_left",
    }
)

STAGES = ("session", "plan", "questions", "interview", "evaluation", "other")


def stage_of(event: str) -> str:
    """Pipeline stage of an event name (see the admin pipeline view)."""
    if event.startswith(("session_created", "session_creation", "session_entry")):
        return "session"
    if event.startswith(("plan_", "project_evidence")):
        return "plan"
    if event.startswith(("question_selection", "question_generated", "spoken_questions")):
        return "questions"
    if event.startswith(("evaluation_", "turn_graded")):
        return "evaluation"
    if event.startswith(
        (
            "voice_",
            "chat_",
            "candidate_message",
            "text_turn",
            "text_runtime",
            "turn_",
            "engine_turn",
            "probe_",
            "hard_timeout",
            "closing_pacing",
            "behavioral_reserve",
            "session_completion",
            "session_closed",
        )
    ):
        return "interview"
    return "other"


def status_of(event: str, fields: dict[str, Any]) -> str:
    """started | completed | error | warning | info."""
    if event.endswith(("_failed", "_error")) or fields.get("error_type"):
        return "error"
    if event in _WARNING_EVENTS:
        return "warning"
    if event.endswith(("_started", "_requested")):
        return "started"
    if event.endswith(("_completed", "_saved", "_processed", "_answered", "_asked")):
        return "completed"
    if event.endswith(
        ("_triggered", "_downgraded", "_capped", "_partial_coverage", "_blocked_by_match_threshold")
    ):
        return "warning"
    return "info"


def _clean(value: Any, depth: int = 0, max_list: int = _MAX_LIST) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= _MAX_STRING else value[: _MAX_STRING - 1] + "…"
    if depth >= 3:
        return str(value)[:_MAX_STRING]
    if isinstance(value, dict):
        return {str(k): _clean(v, depth + 1, max_list) for k, v in list(value.items())[:max_list]}
    if isinstance(value, (list, tuple, set)):
        return [_clean(v, depth + 1, max_list) for v in list(value)[:max_list]]
    return str(value)[:_MAX_STRING]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


@dataclass
class _Recorder:
    events: deque = field(default_factory=lambda: deque(maxlen=_BUFFER_LIMIT))
    calls: deque = field(default_factory=lambda: deque(maxlen=_BUFFER_LIMIT))
    # Matching run events and the model calls made during a run.
    match_events: deque = field(default_factory=lambda: deque(maxlen=_BUFFER_LIMIT))
    match_calls: deque = field(default_factory=lambda: deque(maxlen=_BUFFER_LIMIT))
    # CV upload/parse events and the model calls made while parsing a CV.
    cv_events: deque = field(default_factory=lambda: deque(maxlen=_BUFFER_LIMIT))
    cv_calls: deque = field(default_factory=lambda: deque(maxlen=_BUFFER_LIMIT))
    lock: threading.Lock = field(default_factory=threading.Lock)
    wake: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    last_retention: float = 0.0


_recorder = _Recorder()

LiveListener = Callable[[list[dict[str, Any]]], None]
_live_listeners: list[LiveListener] = []


def add_live_listener(listener: LiveListener) -> None:
    """Call ``listener(summaries)`` from the flusher thread after each write."""
    if listener not in _live_listeners:
        _live_listeners.append(listener)


def remove_live_listener(listener: LiveListener) -> None:
    if listener in _live_listeners:
        _live_listeners.remove(listener)


def enabled() -> bool:
    from src.core.config import get_settings

    configured = get_settings().pipeline_events_enabled
    if configured is not None:
        return bool(configured)
    # Unit tests run without a database; they opt in explicitly when needed.
    return "pytest" not in sys.modules


def _ensure_thread() -> None:
    if _recorder.thread is not None and _recorder.thread.is_alive():
        return
    with _recorder.lock:
        if _recorder.thread is not None and _recorder.thread.is_alive():
            return
        thread = threading.Thread(target=_run_flusher, name="pipeline-events-flusher", daemon=True)
        _recorder.thread = thread
        thread.start()


def record_event(workflow: str, event: str, fields: dict[str, Any]) -> None:
    """Buffer one pipeline event when it belongs to an interview session.

    Matching and clarification events go to the matching run instead (see
    ``record_matching_event``).
    """
    if workflow in MATCHING_WORKFLOWS:
        record_matching_event(event, fields)
        return
    if workflow in CV_WORKFLOWS:
        record_cv_event(event, fields)
        return
    if workflow not in PERSISTED_WORKFLOWS:
        return
    session_id = fields.get("session_id")
    if session_id:
        current_session_id.set(str(session_id))
    else:
        session_id = current_session_id.get()
    if not session_id:
        return
    try:
        if not enabled():
            return
        payload = {k: _clean(v) for k, v in fields.items() if k not in _DROPPED_FIELDS and k != "session_id"}
        row = {
            "session_id": str(session_id)[:36],
            "stage": stage_of(event),
            "event": event[:64],
            "status": status_of(event, fields),
            "turn_index": _int_or_none(fields.get("turn_index")),
            "duration_ms": _int_or_none(fields.get("duration_ms") or fields.get("core_ms")),
            "payload": json.dumps(payload, ensure_ascii=False, default=str),
            "created_at": datetime.now(UTC),
        }
        _recorder.events.append(row)
        _ensure_thread()
        _recorder.wake.set()
    except Exception:  # noqa: BLE001 - recording must never break the pipeline
        logger.debug("pipeline event dropped", exc_info=True)


def _strip_matching(value: Any, depth: int = 0) -> Any:
    if isinstance(value, dict):
        return {
            k: _strip_matching(v, depth + 1)
            for k, v in value.items()
            if str(k) not in _MATCHING_DROPPED_FIELDS
        }
    if isinstance(value, (list, tuple)) and depth < 4:
        return [_strip_matching(v, depth + 1) for v in value]
    return value


def matching_status_of(event: str, fields: dict[str, Any]) -> str:
    """started | completed | error | warning | info | skipped for a matching event."""
    explicit = fields.get("event_status")
    if explicit in MATCHING_STATUSES:
        return str(explicit)
    if event.endswith("_failed") or fields.get("error_type"):
        return "error"
    if event.endswith("_skipped"):
        return "skipped"
    if event.endswith("_requested"):
        return "started"
    if event.endswith(("_completed", "_resolved", "_evaluated", "_scored")):
        return "completed"
    return "info"


def matching_payload(fields: dict[str, Any]) -> dict[str, Any]:
    """The stored payload of a matching event: no text-bearing keys, no row columns."""
    return {k: _clean(v, max_list=_MATCHING_MAX_LIST) for k, v in _strip_matching(fields).items()}


def record_matching_event(event: str, fields: dict[str, Any]) -> None:
    """Buffer one matching event for the active match run (dropped without one)."""
    run = current_match_run.get()
    if run is None:
        return
    stage = MATCHING_EVENT_STAGES.get(event)
    if stage is None:
        return
    try:
        if not enabled():
            return
        row = {
            "run_id": run.run_id[:36],
            "flow": run.flow[:24],
            "cache_key": run.cache_key,
            "user_id": (str(run.user_id)[:36] if run.user_id else None),
            "resume_id": (str(run.resume_id)[:64] if run.resume_id else None),
            "job_id": (str(run.job_id)[:64] if run.job_id else None),
            "stage": stage,
            "event": event[:64],
            "status": matching_status_of(event, fields),
            "duration_ms": _int_or_none(fields.get("duration_ms")),
            "payload": json.dumps(matching_payload(fields), ensure_ascii=False, default=str),
            "created_at": datetime.now(UTC),
        }
        _recorder.match_events.append(row)
        _ensure_thread()
        _recorder.wake.set()
    except Exception:  # noqa: BLE001 - recording must never break matching
        logger.debug("matching event dropped", exc_info=True)


def _strip_cv(value: Any, depth: int = 0) -> Any:
    if isinstance(value, dict):
        return {k: _strip_cv(v, depth + 1) for k, v in value.items() if str(k) not in _CV_DROPPED_FIELDS}
    if isinstance(value, (list, tuple)) and depth < 4:
        return [_strip_cv(v, depth + 1) for v in value]
    return value


def cv_payload(fields: dict[str, Any]) -> dict[str, Any]:
    """The stored payload of a CV event: no text-bearing keys, no row columns."""
    return {
        k: _clean(v)
        for k, v in _strip_cv(fields).items()
        if k not in {"event_status"}
    }


def cv_status_of(event: str, fields: dict[str, Any]) -> str:
    """started | completed | error | warning | info | skipped for a CV event."""
    explicit = fields.get("event_status")
    if explicit in MATCHING_STATUSES:
        return str(explicit)
    if event == "quality_gate":
        return "completed" if str(fields.get("status") or "") in _QUALITY_OK else "warning"
    return CV_EVENT_STATUSES.get(event, "info")


def record_cv_event(event: str, fields: dict[str, Any]) -> None:
    """Buffer one CV event for the active CV context (dropped without one)."""
    run = current_cv_run.get()
    if run is None:
        return
    stage = CV_EVENT_STAGES.get(event)
    if stage is None:
        return
    try:
        if not enabled():
            return
        row = {
            "cv_id": str(run.cv_id)[:36],
            "attempt_id": str(run.attempt_id)[:36],
            "user_id": (str(run.user_id)[:36] if run.user_id else None),
            "stage": stage,
            "event": event[:64],
            "status": cv_status_of(event, fields),
            "duration_ms": _int_or_none(fields.get("duration_ms")),
            "payload": json.dumps(cv_payload(fields), ensure_ascii=False, default=str),
            "created_at": datetime.now(UTC),
        }
        _recorder.cv_events.append(row)
        _ensure_thread()
        _recorder.wake.set()
    except Exception:  # noqa: BLE001 - recording must never break CV parsing
        logger.debug("cv event dropped", exc_info=True)


def record_ai_call(
    *,
    purpose: str,
    provider: str,
    model: str | None,
    status: str,
    duration_ms: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    input_chars: int | None = None,
    output_chars: int | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    input_text: str | None = None,
    output_text: str | None = None,
) -> None:
    """Buffer one model call. Text is kept only when capture is configured."""
    try:
        if not enabled():
            return
        from src.core.config import get_settings

        capture = bool(get_settings().ai_call_log_capture_text)
        row = {
            "session_id": current_session_id.get(),
            "purpose": purpose[:160],
            "provider": provider[:32],
            "model": (model or "")[:120] or None,
            "status": status[:12],
            "duration_ms": int(duration_ms),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_chars": input_chars,
            "output_chars": output_chars,
            "error_type": (error_type or "")[:120] or None,
            "error_message": (error_message or "")[:500] or None,
            "input_preview": (input_text or "")[:2000] if capture and input_text else None,
            "output_preview": (output_text or "")[:2000] if capture and output_text else None,
            "created_at": datetime.now(UTC),
        }
        run = current_match_run.get()
        cv_run = current_cv_run.get() if run is None else None
        if run is not None:
            row["match_run_id"] = run.run_id[:36]
            _recorder.match_calls.append(row)
        elif cv_run is not None:
            row["cv_id"] = str(cv_run.cv_id)[:36]
            cv_run.ai_calls += 1
            cv_run.ai_errors += status == "error"
            _recorder.cv_calls.append(row)
        else:
            _recorder.calls.append(row)
        _ensure_thread()
        _recorder.wake.set()
    except Exception:  # noqa: BLE001
        logger.debug("ai call log dropped", exc_info=True)


class ai_call_timer:
    """``with ai_call_timer(purpose, provider, model) as call: ...; call.done(text, usage)``.

    Records one ``ai_call_logs`` row when the block exits, with the error when
    it raised. ``done`` is optional; without it the call is recorded as ok
    with no output size.
    """

    def __init__(self, purpose: str, *, provider: str, model: str | None, input_text: str | None = None):
        self.purpose = purpose
        self.provider = provider
        self.model = model
        self.input_text = input_text
        self.output_text: str | None = None
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self._start = 0.0

    def __enter__(self) -> ai_call_timer:
        self._start = time.perf_counter()
        return self

    def done(self, output_text: str | None = None, usage: Any = None) -> None:
        self.output_text = output_text
        if usage is not None:
            self.input_tokens = _int_or_none(
                getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", None)
            )
            self.output_tokens = _int_or_none(
                getattr(usage, "output_tokens", None) or getattr(usage, "completion_tokens", None)
            )

    def __exit__(self, exc_type, exc, tb) -> bool:
        record_ai_call(
            purpose=self.purpose,
            provider=self.provider,
            model=self.model,
            status="error" if exc is not None else "ok",
            duration_ms=int((time.perf_counter() - self._start) * 1000),
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            input_chars=len(self.input_text) if self.input_text is not None else None,
            output_chars=len(self.output_text) if self.output_text is not None else None,
            error_type=type(exc).__name__ if exc is not None else None,
            error_message=str(exc) if exc is not None else None,
            input_text=self.input_text,
            output_text=self.output_text,
        )
        return False


def caller_purpose(depth: int = 2) -> str:
    """``area.file.Function`` of the code that called the model wrapper.

    ``interviews.interview_engine.InterviewCoreEngine._generate_probe_question``:
    the module area keeps same-named files apart (CV and JD both parse in
    ``hybrid.py``).
    """
    try:
        frame = sys._getframe(depth)  # noqa: SLF001
        parts = str(frame.f_globals.get("__name__", "")).split(".")
        if len(parts) > 3 and parts[:2] == ["src", "modules"]:
            module = f"{parts[2]}.{parts[-1]}"
        else:
            module = parts[-1]
        name = getattr(frame.f_code, "co_qualname", frame.f_code.co_name)
        return f"{module}.{name}"
    except (ValueError, AttributeError):
        return "unknown"


# --------------------------------------------------------------------------- flush

_INSERT_EVENTS = """
    INSERT INTO interview_pipeline_events
      (session_id, stage, event, status, turn_index, duration_ms, payload, created_at)
    VALUES (:session_id, :stage, :event, :status, :turn_index, :duration_ms,
            CAST(:payload AS JSONB), :created_at)
"""
_INSERT_CALLS = """
    INSERT INTO ai_call_logs
      (session_id, purpose, provider, model, status, duration_ms, input_tokens, output_tokens,
       input_chars, output_chars, error_type, error_message, input_preview, output_preview, created_at)
    VALUES (:session_id, :purpose, :provider, :model, :status, :duration_ms, :input_tokens, :output_tokens,
       :input_chars, :output_chars, :error_type, :error_message, :input_preview, :output_preview, :created_at)
"""

_INSERT_MATCH_EVENTS = """
    INSERT INTO matching_pipeline_events
      (run_id, flow, cache_key, user_id, resume_id, job_id, stage, event, status, duration_ms,
       payload, created_at)
    VALUES (:run_id, :flow, :cache_key, :user_id, :resume_id, :job_id, :stage, :event, :status,
            :duration_ms, CAST(:payload AS JSONB), :created_at)
"""
_INSERT_MATCH_CALLS = """
    INSERT INTO ai_call_logs
      (session_id, match_run_id, purpose, provider, model, status, duration_ms, input_tokens,
       output_tokens, input_chars, output_chars, error_type, error_message, input_preview,
       output_preview, created_at)
    VALUES (:session_id, :match_run_id, :purpose, :provider, :model, :status, :duration_ms,
       :input_tokens, :output_tokens, :input_chars, :output_chars, :error_type, :error_message,
       :input_preview, :output_preview, :created_at)
"""

_INSERT_CV_EVENTS = """
    INSERT INTO cv_pipeline_events
      (cv_id, attempt_id, user_id, stage, event, status, duration_ms, payload, created_at)
    VALUES (:cv_id, :attempt_id, :user_id, :stage, :event, :status, :duration_ms,
            CAST(:payload AS JSONB), :created_at)
"""
_INSERT_CV_CALLS = """
    INSERT INTO ai_call_logs
      (session_id, cv_id, purpose, provider, model, status, duration_ms, input_tokens,
       output_tokens, input_chars, output_chars, error_type, error_message, input_preview,
       output_preview, created_at)
    VALUES (:session_id, :cv_id, :purpose, :provider, :model, :status, :duration_ms,
       :input_tokens, :output_tokens, :input_chars, :output_chars, :error_type, :error_message,
       :input_preview, :output_preview, :created_at)
"""


def live_summaries(events: list[dict], calls: list[dict]) -> list[dict[str, Any]]:
    """One short summary per session of what was just written.

    ``{"sessionId", "at", "events": [{event, stage, status, turnIndex}], "aiCalls",
    "aiErrors", "problems"}``; model calls made outside an interview are grouped
    under ``sessionId: None``.
    """
    groups: dict[str | None, dict[str, Any]] = {}

    def group(session_id: str | None) -> dict[str, Any]:
        if session_id not in groups:
            groups[session_id] = {
                "sessionId": session_id,
                "at": datetime.now(UTC).isoformat(),
                "events": [],
                "aiCalls": 0,
                "aiErrors": 0,
                "problems": 0,
            }
        return groups[session_id]

    for row in events:
        item = group(row.get("session_id"))
        item["events"].append(
            {
                "event": row.get("event"),
                "stage": row.get("stage"),
                "status": row.get("status"),
                "turnIndex": row.get("turn_index"),
            }
        )
        if row.get("status") in {"warning", "error"}:
            item["problems"] += 1
    for row in calls:
        item = group(row.get("session_id"))
        item["aiCalls"] += 1
        if row.get("status") == "error":
            item["aiErrors"] += 1
            item["problems"] += 1
    for item in groups.values():
        item["events"] = item["events"][-_SUMMARY_EVENTS:]
    return list(groups.values())


def notify_payload(summary: dict[str, Any]) -> str:
    """The pg_notify payload for one summary, trimmed under the size limit."""
    body = {"o": INSTANCE_ID, **summary}
    text_ = json.dumps(body, ensure_ascii=False, default=str)
    while len(text_.encode("utf-8")) > _NOTIFY_MAX_BYTES and body["events"]:
        body["events"] = body["events"][len(body["events"]) // 2 + 1 :]
        text_ = json.dumps(body, ensure_ascii=False, default=str)
    return text_


def _drain_matching() -> tuple[list[dict], list[dict]]:
    events: list[dict] = []
    calls: list[dict] = []
    while _recorder.match_events:
        events.append(_recorder.match_events.popleft())
    while _recorder.match_calls:
        calls.append(_recorder.match_calls.popleft())
    return events, calls


def _drain_cv() -> tuple[list[dict], list[dict]]:
    events: list[dict] = []
    calls: list[dict] = []
    while _recorder.cv_events:
        events.append(_recorder.cv_events.popleft())
    while _recorder.cv_calls:
        calls.append(_recorder.cv_calls.popleft())
    return events, calls


def _drain() -> tuple[list[dict], list[dict]]:
    events: list[dict] = []
    calls: list[dict] = []
    while _recorder.events:
        events.append(_recorder.events.popleft())
    while _recorder.calls:
        calls.append(_recorder.calls.popleft())
    return events, calls


async def _write(
    events: list[dict], calls: list[dict], *, retention: bool, summaries: list[dict] | None = None
) -> None:
    from sqlalchemy import text

    from src.core.config import get_settings
    from src.infrastructure.database import StandaloneSessionFactory

    async with StandaloneSessionFactory() as db:
        if events:
            await db.execute(text(_INSERT_EVENTS), events)
        if calls:
            await db.execute(text(_INSERT_CALLS), calls)
        for summary in summaries or []:
            # Delivered on commit, so listeners never read before the rows exist.
            await db.execute(
                text("SELECT pg_notify(:channel, :payload)"),
                {"channel": NOTIFY_CHANNEL, "payload": notify_payload(summary)},
            )
        if retention:
            days = int(get_settings().pipeline_events_retention_days)
            await db.execute(
                text(
                    "DELETE FROM interview_pipeline_events "
                    "WHERE created_at < now() - make_interval(days => :d)"
                ),
                {"d": days},
            )
            await db.execute(
                text("DELETE FROM ai_call_logs WHERE created_at < now() - make_interval(days => :d)"),
                {"d": days},
            )
        await db.commit()


async def _write_matching(events: list[dict], calls: list[dict], *, retention: bool) -> None:
    """Matching run events and run-stamped model calls, in their own transaction."""
    from sqlalchemy import text

    from src.core.config import get_settings
    from src.infrastructure.database import StandaloneSessionFactory

    async with StandaloneSessionFactory() as db:
        if events:
            await db.execute(text(_INSERT_MATCH_EVENTS), events)
        if calls:
            await db.execute(text(_INSERT_MATCH_CALLS), calls)
        if retention:
            days = int(get_settings().pipeline_events_retention_days)
            await db.execute(
                text(
                    "DELETE FROM matching_pipeline_events "
                    "WHERE created_at < now() - make_interval(days => :d)"
                ),
                {"d": days},
            )
        await db.commit()


async def _flush_matching(events: list[dict], calls: list[dict], *, retention: bool) -> int:
    if not events and not calls and not retention:
        return 0
    try:
        await _write_matching(events, calls, retention=retention)
        return len(events) + len(calls)
    except Exception:  # noqa: BLE001 - table missing before migration 0032, or DB down
        logger.warning(
            "Could not store %d matching events and %d matching AI call logs",
            len(events),
            len(calls),
            exc_info=True,
        )
    if not calls:
        return 0
    # Keep the model calls even when the matching tables are not migrated.
    plain = [{k: v for k, v in row.items() if k != "match_run_id"} for row in calls]
    try:
        await _write([], plain, retention=False)
        return len(plain)
    except Exception:  # noqa: BLE001
        logger.warning("Could not store %d matching AI call logs", len(plain), exc_info=True)
        return 0


async def _write_cv(events: list[dict], calls: list[dict], *, retention: bool) -> None:
    """CV pipeline events and CV-stamped model calls, in their own transaction."""
    from sqlalchemy import text

    from src.core.config import get_settings
    from src.infrastructure.database import StandaloneSessionFactory

    async with StandaloneSessionFactory() as db:
        if events:
            await db.execute(text(_INSERT_CV_EVENTS), events)
        if calls:
            await db.execute(text(_INSERT_CV_CALLS), calls)
        if retention:
            days = int(get_settings().pipeline_events_retention_days)
            await db.execute(
                text("DELETE FROM cv_pipeline_events WHERE created_at < now() - make_interval(days => :d)"),
                {"d": days},
            )
        await db.commit()


async def _flush_cv(events: list[dict], calls: list[dict], *, retention: bool) -> int:
    if not events and not calls and not retention:
        return 0
    try:
        await _write_cv(events, calls, retention=retention)
        return len(events) + len(calls)
    except Exception:  # noqa: BLE001 - table missing before migration 0033, or DB down
        logger.warning(
            "Could not store %d CV pipeline events and %d CV AI call logs",
            len(events),
            len(calls),
            exc_info=True,
        )
    if not calls:
        return 0
    # Keep the model calls even when the CV tables are not migrated.
    plain = [{k: v for k, v in row.items() if k != "cv_id"} for row in calls]
    try:
        await _write([], plain, retention=False)
        return len(plain)
    except Exception:  # noqa: BLE001
        logger.warning("Could not store %d CV AI call logs", len(plain), exc_info=True)
        return 0


async def flush_once(*, retention: bool = False) -> int:
    events, calls = _drain()
    match_events, match_calls = _drain_matching()
    cv_events, cv_calls = _drain_cv()
    match_written = await _flush_matching(match_events, match_calls, retention=retention)
    cv_written = await _flush_cv(cv_events, cv_calls, retention=retention)
    matching_written = match_written + cv_written
    # Model calls made during a match run or a CV parse are live-summarised like
    # any other call (an interview planner's calls still reach that session's live view).
    written_match_calls = (match_calls if match_written else []) + (cv_calls if cv_written else [])
    if not events and not calls and not written_match_calls and not retention:
        return matching_written
    summary_calls = calls + written_match_calls
    summaries = live_summaries(events, summary_calls) if events or summary_calls else []
    try:
        await _write(events, calls, retention=retention, summaries=summaries)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not store %d pipeline events and %d AI call logs", len(events), len(calls), exc_info=True
        )
        return matching_written
    if summaries:
        for listener in list(_live_listeners):
            try:
                listener(summaries)
            except Exception:  # noqa: BLE001 - a broken listener must not stop recording
                logger.debug("pipeline live listener failed", exc_info=True)
    return len(events) + len(calls) + matching_written


def _run_flusher() -> None:
    async def loop() -> None:
        while True:
            await asyncio.to_thread(_recorder.wake.wait, _FLUSH_INTERVAL_SECONDS * 30)
            _recorder.wake.clear()
            # Small debounce so a burst of events lands in one insert.
            await asyncio.sleep(_DEBOUNCE_SECONDS)
            now = time.monotonic()
            retention = now - _recorder.last_retention > _RETENTION_CHECK_SECONDS
            if retention:
                _recorder.last_retention = now
            await flush_once(retention=retention)

    try:
        asyncio.run(loop())
    except Exception:  # noqa: BLE001
        logger.exception("Pipeline event flusher stopped")


@atexit.register
def _flush_at_exit() -> None:
    flush_pending()


def has_pending() -> bool:
    return bool(
        _recorder.events
        or _recorder.calls
        or _recorder.match_events
        or _recorder.match_calls
        or _recorder.cv_events
        or _recorder.cv_calls
    )


def flush_pending() -> None:
    """Write whatever is buffered now, synchronously (process exit, Celery child shutdown)."""
    if not has_pending():
        return
    try:
        asyncio.run(flush_once())
    except Exception:  # noqa: BLE001
        pass
