"""Langfuse tracing of the interview AI: what the model did, whether it was right, whether it strayed.

Design: DOC_AND_PLAN/plan/interview/LLM_MONITORING_DESIGN_2026-10-25.md.

One Langfuse **trace per interview turn** (``interview_turn``) and one per
**grading run** (``session_grading``), grouped by the interview session:

    trace interview_turn  (session_id = interview session, user_id = hashed)
     ├─ generation evaluate_answer / probe / clarify / qna   (src/modules/ai/facade.generate_text)
     ├─ input  {question, stage, answer*}
     ├─ output {action, decision_rule, intent, sufficiency, missing_aspect, reply, guard drafts*}
     └─ scores rule.flagged, flag.<name>, guard.rejects     (+ judge and human scores in Langfuse)

\\* Text the candidate wrote (and prompts that contain it) is sent only for a
share of sessions (``LANGFUSE_TEXT_SAMPLE_RATE``) and for flagged turns, and
always through ``mask`` (emails, phone numbers, the candidate's name). The
LLM judge configured in Langfuse runs on the traces tagged ``text``.

Everything here is optional and never raises: without keys or without the
``langfuse`` package every function is a cheap no-op, and an exporter error
never reaches the interview.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import re
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

TURN_TRACE = "interview_turn"
GRADING_TRACE = "session_grading"

# Step names shown in Langfuse, from the code that called the model
# (``caller_purpose``: ``area.file.Function``). Unknown callers keep the purpose.
STEP_BY_CALLER: dict[str, str] = {
    "_evaluate_candidate_response": "evaluate_answer",
    "_generate_probe_question": "probe",
    "_generate_clarify_text": "clarify",
    "_answer_closing_question": "qna",
    "_handle_closing_turn": "qna",
    "add_spoken_versions": "spoken_rewrite",
    "_rewrite_batch": "spoken_rewrite",
}


@dataclass
class TraceState:
    """The open trace of the current turn or grading run."""

    name: str
    trace_id: str
    session_id: str
    capture_text: bool
    span: Any = None
    flags: list[str] = field(default_factory=list)
    # What the model wrote, kept for a flagged turn whose prompts were not sent.
    outputs: list[dict[str, Any]] = field(default_factory=list)


_current: contextvars.ContextVar[TraceState | None] = contextvars.ContextVar("llm_trace", default=None)
_lock = threading.Lock()
_client: Any = None
_client_ready = False


# --------------------------------------------------------------------------- client


def enabled() -> bool:
    from src.core.config import get_settings

    settings = get_settings()
    if settings.langfuse_enabled is False:
        return False
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


def client() -> Any:
    """The Langfuse client, or None when tracing is off or the package is missing."""
    global _client, _client_ready
    if _client_ready:
        return _client
    with _lock:
        if _client_ready:
            return _client
        _client_ready = True
        if not enabled():
            return None
        try:
            from langfuse import Langfuse

            from src.core.config import get_settings

            settings = get_settings()
            _client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                base_url=settings.langfuse_base_url,
                environment=(settings.langfuse_environment or settings.app_env or "development").casefold()[
                    :40
                ],
                mask=mask,
            )
        except Exception:  # noqa: BLE001 - tracing is optional
            logger.warning("Langfuse tracing disabled: client could not start", exc_info=True)
            _client = None
        return _client


def use_client(langfuse_client: Any) -> None:
    """Install a client (tests: a Langfuse instance with an in-memory span exporter)."""
    global _client, _client_ready
    with _lock:
        _client, _client_ready = langfuse_client, True


def flush() -> None:
    lf = _client
    if lf is not None:
        try:
            lf.flush()
        except Exception:  # noqa: BLE001
            logger.debug("Langfuse flush failed", exc_info=True)


# --------------------------------------------------------------------------- privacy

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Vietnamese mobile and landline numbers, with or without +84 and separators.
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?84|0)(?:[\s.-]?\d){8,10}(?!\d)")
# Names of candidates in recent turns (full names and call names), masked in every payload.
_NAMES: deque[str] = deque(maxlen=200)


def register_names(*names: str | None) -> None:
    for name in names:
        value = " ".join(str(name or "").split())
        if len(value) >= 2 and value not in _NAMES:
            _NAMES.append(value)


def _mask_text(value: str) -> str:
    value = _EMAIL_RE.sub("[email]", value)
    value = _PHONE_RE.sub("[sđt]", value)
    for name in sorted(_NAMES, key=len, reverse=True):
        value = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", "[tên]", value)
    return value


def mask(*, data: Any, **_: Any) -> Any:
    """Langfuse mask hook: applied to every input, output and metadata value."""
    if isinstance(data, str):
        return _mask_text(data)
    if isinstance(data, dict):
        return {key: mask(data=value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [mask(data=value) for value in data]
    return data


def _share(seed: str) -> float:
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF


def text_sampled(session_id: str) -> bool:
    """Whether this session's prompts and answers are sent as text (stable per session)."""
    from src.core.config import get_settings

    return _share(f"text:{session_id}") < get_settings().langfuse_text_sample_rate


def hashed_user(user_id: str | None) -> str | None:
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:16] if user_id else None


def current() -> TraceState | None:
    return _current.get()


def current_trace_id() -> str | None:
    state = _current.get()
    return state.trace_id if state else None


# --------------------------------------------------------------------------- traces


@contextmanager
def trace(
    name: str,
    *,
    session_id: str,
    user_id: str | None = None,
    input: dict[str, Any] | None = None,
    private_input: dict[str, Any] | None = None,
    metadata: dict[str, str] | None = None,
    tags: list[str] | None = None,
) -> Iterator[TraceState | None]:
    """Open one trace (a turn or a grading run). Yields None when tracing is off.

    ``private_input`` (the candidate's answer) is sent only when the session is
    text-sampled, or later by ``finish`` when the turn is flagged.
    """
    lf = client()
    if lf is None or _current.get() is not None:
        yield None
        return
    try:
        from langfuse import propagate_attributes

        capture = text_sampled(session_id)
        state = TraceState(
            name=name, trace_id=lf.create_trace_id(), session_id=session_id, capture_text=capture
        )
        attrs = {
            "session_id": str(session_id)[:200],
            "user_id": hashed_user(user_id),
            "trace_name": name,
            "tags": [*(tags or []), *(["text"] if capture else [])],
            "metadata": {k: str(v)[:200] for k, v in (metadata or {}).items() if v is not None},
        }
        span_cm = lf.start_as_current_observation(
            name=name,
            as_type="span",
            trace_context={"trace_id": state.trace_id},
            input={**(input or {}), **((private_input or {}) if capture else {})},
            metadata=metadata,
        )
        attrs_cm = propagate_attributes(**{k: v for k, v in attrs.items() if v})
    except Exception:  # noqa: BLE001
        logger.debug("Langfuse trace not opened", exc_info=True)
        yield None
        return

    with span_cm as span, attrs_cm:
        state.span = span
        token = _current.set(state)
        try:
            yield state
        finally:
            _current.reset(token)


def finish(
    state: TraceState | None,
    *,
    output: dict[str, Any],
    flags: list[str],
    private_input: dict[str, Any] | None = None,
    scores: dict[str, float] | None = None,
) -> None:
    """Close a turn: output, flags as tags and scores, and the text of a flagged turn."""
    if state is None or state.span is None:
        return
    lf = client()
    try:
        from langfuse import propagate_attributes

        state.flags = sorted(set(state.flags) | set(flags))
        update: dict[str, Any] = {"output": output}
        if state.flags and not state.capture_text:
            # The prompts of this turn were sent without text; keep what the
            # judge and the reviewer need on the turn itself.
            update["input"] = {**(private_input or {})}
            update["output"] = {**output, "model_outputs": state.outputs[-6:]}
        tags = [f"flag:{flag}" for flag in state.flags] + (["flagged", "text"] if state.flags else [])
        with propagate_attributes(tags=tags) if tags else _nullcontext():
            state.span.update(**update, level="WARNING" if state.flags else None)
        if lf is not None:
            lf.create_score(
                trace_id=state.trace_id,
                name="rule.flagged",
                value=1 if state.flags else 0,
                data_type="BOOLEAN",
            )
            for flag in state.flags:
                lf.create_score(trace_id=state.trace_id, name=f"flag.{flag}", value=1, data_type="BOOLEAN")
            for name, value in (scores or {}).items():
                lf.create_score(trace_id=state.trace_id, name=name, value=float(value), data_type="NUMERIC")
        _queue_for_review(state)
    except Exception:  # noqa: BLE001
        logger.debug("Langfuse trace not finished", exc_info=True)


@contextmanager
def _nullcontext() -> Iterator[None]:
    yield None


def _queue_for_review(state: TraceState) -> None:
    """Flagged turns, and a random share of the text-sampled ones, go to the review queue."""
    from src.core.config import get_settings

    settings = get_settings()
    queue_id = settings.langfuse_review_queue_id
    if not queue_id:
        return
    random_pick = (
        state.capture_text and _share(f"review:{state.trace_id}") < settings.langfuse_review_random_rate
    )
    if not (state.flags or random_pick):
        return
    lf = client()
    if lf is None:
        return

    def add() -> None:
        try:
            lf.api.annotation_queues.create_queue_item(
                queue_id, object_id=state.trace_id, object_type="TRACE"
            )
        except Exception:  # noqa: BLE001 - the trace may not be ingested yet; the flag tag still finds it
            logger.debug("Langfuse review queue item not created", exc_info=True)

    # The trace must exist before it is queued: wait for the exporter.
    timer = threading.Timer(15.0, add)
    timer.daemon = True
    timer.start()


def score_trace(
    trace_id: str | None, name: str, value: float, *, comment: str | None = None, data_type: str = "BOOLEAN"
) -> None:
    """A score on any trace (an admin's verdict from our own pages, a post-session check)."""
    lf = client()
    if lf is None or not trace_id:
        return
    try:
        lf.create_score(trace_id=trace_id, name=name, value=value, data_type=data_type, comment=comment)
    except Exception:  # noqa: BLE001
        logger.debug("Langfuse score not created", exc_info=True)


def score_session(session_id: str, name: str, value: float, *, comment: str | None = None) -> None:
    lf = client()
    if lf is None:
        return
    try:
        lf.create_score(session_id=session_id, name=name, value=value, data_type="NUMERIC", comment=comment)
    except Exception:  # noqa: BLE001
        logger.debug("Langfuse session score not created", exc_info=True)


def add_flag(flag: str) -> None:
    state = _current.get()
    if state is not None and flag not in state.flags:
        state.flags.append(flag)


def project_url() -> str | None:
    """``<Langfuse UI>/project/<id>`` for links on the admin pages; None when off or unreachable."""
    lf = client()
    if lf is None:
        return None
    try:
        from src.core.config import get_settings

        project_id = lf._get_project_id()  # noqa: SLF001 - cached by the SDK after the first call
        if not project_id:
            return None
        settings = get_settings()
        base = (settings.langfuse_ui_url or settings.langfuse_base_url).rstrip("/")
        return f"{base}/project/{project_id}"
    except Exception:  # noqa: BLE001
        logger.debug("Langfuse project not reachable", exc_info=True)
        return None


# --------------------------------------------------------------------------- generations


def step_of(purpose: str) -> str:
    function = str(purpose or "").rsplit(".", 1)[-1]
    return STEP_BY_CALLER.get(function, function or "llm_call")


def prompt_version(instructions: str) -> str:
    """Short hash of the system prompt: changes whenever the wording does."""
    return hashlib.sha1(str(instructions or "").encode("utf-8")).hexdigest()[:8]


class Generation:
    """One model call inside the current trace (no-op outside a trace)."""

    def __init__(
        self,
        purpose: str,
        *,
        model: str,
        instructions: str,
        input_text: str,
        options: dict[str, Any] | None = None,
    ):
        self.state = _current.get()
        self.step = step_of(purpose)
        self.observation = None
        parent = self.state.span if self.state is not None else None
        capture = bool(self.state and self.state.capture_text)
        if parent is None:
            # Inside a Langfuse experiment (scripts/langfuse_experiment.py) the
            # SDK's own span is current: nest under it, with text (synthetic data).
            lf = client()
            try:
                if lf is None or not lf.get_current_trace_id():
                    return
            except Exception:  # noqa: BLE001
                return
            parent, capture = lf, True
        try:
            self.observation = parent.start_observation(
                name=self.step,
                as_type="generation",
                model=model,
                version=prompt_version(instructions),
                input=(
                    [{"role": "system", "content": instructions}, {"role": "user", "content": input_text}]
                    if capture
                    else None
                ),
                model_parameters={
                    k: v for k, v in (options or {}).items() if isinstance(v, (str, int, float, bool))
                },
                metadata={"purpose": purpose, "prompt_id": self.step, "text_captured": capture},
            )
        except Exception:  # noqa: BLE001
            self.observation = None
        self.capture = capture

    def end(
        self, *, output: str | None = None, usage: Any = None, error: BaseException | None = None
    ) -> None:
        if self.observation is None:
            return
        try:
            details = {}
            if usage is not None:
                for key, attr in (("input", "input_tokens"), ("output", "output_tokens")):
                    value = getattr(usage, attr, None)
                    if isinstance(value, int):
                        details[key] = value
            capture = self.capture
            self.observation.update(
                output=output if capture else None,
                usage_details=details or None,
                level="ERROR" if error else None,
                status_message=f"{type(error).__name__}: {error}"[:300] if error else None,
            )
            self.observation.end()
            if self.state is not None and output and not capture:
                self.state.outputs.append({"step": self.step, "output": output[:1200]})
        except Exception:  # noqa: BLE001
            logger.debug("Langfuse generation not closed", exc_info=True)
