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
``langfuse`` package every function is a cheap no-op.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from src.infrastructure.langfuse.connection import client
from src.infrastructure.langfuse.privacy import hashed_user, share, text_sampled

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
        state.capture_text and share(f"review:{state.trace_id}") < settings.langfuse_review_random_rate
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
            # When an SDK span of its own is current (a harness driving the engine
            # outside a request), nest under it, with text: that data is synthetic.
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
