"""What the voice room is doing, reported by the LiveKit agent for the admin view.

The agent runs in its own process (the voice-agent container) and reaches
Interview Core over HTTP, so it cannot write pipeline events itself. It
reports them to ``POST /ai/voice-lab/livekit/runtime/events`` with the
session-scoped runtime credential, and the API records them like any other
interview event (src/core/pipeline_events.py).

Two kinds of event:

* ``voice_state`` - where the conversation is right now: the AI is speaking,
  waiting for an answer, the candidate is speaking, the answer is being
  submitted, Interview Core is processing it, or the candidate left;
* incidents and measurements - the candidate was not heard, a question was
  replayed, the voice provider failed over, how long synthesis took.

Only numbers, flags and short codes are sent: never what anyone said.
Reporting never blocks or breaks the call: events are batched and posted in
the background, and a failed post is dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

VOICE_STATES = (
    "ai_speaking",
    "waiting_answer",
    "candidate_speaking",
    "answer_pending",
    "processing",
    "candidate_away",
)

VOICE_EVENTS = frozenset(
    {
        "voice_session_started",
        "voice_state",
        "voice_answer_submitted",
        "voice_core_completed",
        "voice_core_failed",
        "voice_not_heard",
        "voice_unclear_transcript",
        "voice_silence_reprompt",
        "voice_question_replayed",
        "voice_interruption",
        "voice_processing_filler",
        "voice_tts_metrics",
        "voice_tts_fallback",
        "voice_provider_degraded",
        "voice_provider_recovered",
        "voice_candidate_left",
        "voice_candidate_returned",
        "voice_session_released",
    }
)

_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# Never forwarded, whatever the agent sends: these would carry speech.
_TEXT_KEYS = frozenset({"text", "transcript", "content", "raw_transcript", "answer", "question"})
_MAX_FIELDS = 12
_MAX_STRING = 120


def sanitize_fields(fields: dict[str, Any] | None) -> dict[str, Any]:
    """Short, flat, speech-free fields: bools, finite numbers and short codes."""
    clean: dict[str, Any] = {}
    for key, value in (fields or {}).items():
        if len(clean) >= _MAX_FIELDS:
            break
        if not isinstance(key, str) or not _KEY.match(key) or key in _TEXT_KEYS:
            continue
        if isinstance(value, bool) or value is None:
            clean[key] = value
        elif isinstance(value, int):
            clean[key] = value
        elif isinstance(value, float):
            if math.isfinite(value):
                clean[key] = round(value, 4)
        elif isinstance(value, str):
            clean[key] = value[:_MAX_STRING]
    return clean


Poster = Callable[[list[dict[str, Any]]], Awaitable[None]]


class VoiceEventReporter:
    """Batches voice events and posts them without ever blocking the caller.

    ``emit`` and ``state`` are synchronous and safe to call from LiveKit
    event handlers; they need a running event loop and do nothing without one.
    """

    def __init__(
        self,
        post: Poster,
        *,
        flush_delay: float = 0.2,
        max_batch: int = 50,
        max_queue: int = 500,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._post = post
        self._flush_delay = flush_delay
        self._max_batch = max_batch
        self._max_queue = max_queue
        self._now = now or (lambda: datetime.now(UTC))
        self._queue: list[dict[str, Any]] = []
        self._task: asyncio.Task | None = None
        self._state: str | None = None
        self._state_kind: str | None = None
        self._closed = False

    @property
    def current_state(self) -> str | None:
        return self._state

    def emit(self, event: str, **fields: Any) -> None:
        if self._closed or event not in VOICE_EVENTS:
            return
        if len(self._queue) >= self._max_queue:
            self._queue.pop(0)  # oldest first: the latest state matters most
        self._queue.append({"event": event, "at": self._now().isoformat(), "fields": sanitize_fields(fields)})
        self._schedule()

    def state(self, state: str, **fields: Any) -> None:
        """Report a state change; repeating the current state is ignored."""
        if state not in VOICE_STATES:
            return
        kind = fields.get("kind")
        if state == self._state and kind == self._state_kind:
            return
        self._state = state
        self._state_kind = kind if isinstance(kind, str) else None
        self.emit("voice_state", state=state, **fields)

    def _schedule(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._flush_soon())

    async def _flush_soon(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(self._flush_delay)
        await self.flush()

    async def flush(self) -> None:
        while self._queue:
            batch, self._queue = self._queue[: self._max_batch], self._queue[self._max_batch :]
            try:
                await self._post(batch)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - monitoring must not affect the call
                logger.debug("Voice events dropped (%d)", len(batch), exc_info=True)

    async def aclose(self) -> None:
        """Send what is queued, then stop accepting events."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        await self.flush()
        self._closed = True
