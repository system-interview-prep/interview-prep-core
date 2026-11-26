"""Speculative work done while the candidate is still speaking.

INTERVIA_INTERVIEW_AGENT_V2_SPEC §7 and the V2 correction brief §5. The
LiveKit runtime used to hold the critical path for up to a second waiting for
a speculative preview to land; §7 is explicit that a pending preview must
never be waited on and the canonical inference must start immediately.

This engine makes that structural rather than a rule to remember:

* :meth:`observe` is the only way work starts, and it is fire-and-forget.
* :meth:`take` is the only way a result is read, and it **never awaits**. A
  preview that has not finished simply is not there, and the caller proceeds.

Everything it produces is read-only and versioned. A result is handed back
only when :mod:`.validator` agrees that the transcript and every version it
was computed against still match, so a preview cannot commit a decision made
from a partial transcript — which §1.1 forbids outright.

The budget is the spec's: debounce, at most one speculative inference in
flight per turn, and a hard cap per turn so a candidate who pauses often
cannot run up provider cost. Cancelling the client does not guarantee the
provider stops billing, so the cap is on calls *started*, not finished.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from time import perf_counter
from typing import Any, Awaitable, Callable

from src.modules.interviews.realtime.anticipation.validator import (
    PreviewKey,
    PreviewRejected,
    validate_preview,
)

logger = logging.getLogger(__name__)

__all__ = ["AnticipationEngine", "AnticipationStats"]

#: Smallest gap between two speculative calls for one turn.
DEBOUNCE_SECONDS = 0.65
#: Hard ceiling on speculative calls within a single turn.
MAX_CALLS_PER_TURN = 3
#: Shortest transcript worth speculating on.
MIN_WORDS = 4


class AnticipationStats:
    """Counters behind the preview hit rate and wasted calls of §13."""

    __slots__ = ("started", "completed", "reused", "discarded", "reasons")

    def __init__(self) -> None:
        self.started = 0
        self.completed = 0
        self.reused = 0
        self.discarded = 0
        self.reasons: dict[str, int] = {}

    def discard(self, reason: str) -> None:
        self.discarded += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        hit_rate = round(self.reused / self.completed, 3) if self.completed else None
        return {
            "started": self.started,
            "completed": self.completed,
            "reused": self.reused,
            "discarded": self.discarded,
            "wasted": max(0, self.started - self.reused),
            "hit_rate": hit_rate,
            "reasons": dict(self.reasons),
        }


class AnticipationEngine:
    """Runs speculative inference off the critical path, for one session.

    ``infer`` is an async read-only callable taking the transcript and the key
    it was computed for, returning whatever the caller wants to reuse (the
    existing runtime hands back a preview token). It must not write session
    state: §7 allows speculation to read, never to commit.
    """

    def __init__(
        self,
        infer: Callable[[str, PreviewKey], Awaitable[Any]],
        *,
        enabled: bool = False,
        debounce_seconds: float = DEBOUNCE_SECONDS,
        max_calls_per_turn: int = MAX_CALLS_PER_TURN,
        min_words: int = MIN_WORDS,
        on_event: Callable[..., None] | None = None,
    ) -> None:
        self._infer = infer
        self._enabled = enabled
        self._debounce = debounce_seconds
        self._max_calls = max_calls_per_turn
        self._min_words = min_words
        self._on_event = on_event

        self._task: asyncio.Task | None = None
        self._pending: tuple[str, PreviewKey] | None = None
        self._result: Any = None
        self._result_key: PreviewKey | None = None
        self._calls_this_turn = 0
        self._last_started = 0.0
        self.stats = AnticipationStats()

    # ------------------------------------------------------------------ turn

    def reset_turn(self) -> None:
        """Forget this turn's speculation; cancel anything still running."""
        self._cancel()
        self._pending = None
        self._result = None
        self._result_key = None
        self._calls_this_turn = 0

    def _cancel(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()

    # --------------------------------------------------------------- observe

    def observe(self, transcript: str, key: PreviewKey) -> None:
        """Note a partial transcript. Returns immediately, always.

        Never awaited by the caller and never awaits itself: an interim
        transcript arrives on the audio path, which must not be slowed by
        speculation about it.
        """
        if not self._enabled:
            return
        text = " ".join(str(transcript or "").split())
        if len(text.split()) < self._min_words:
            return
        if self._calls_this_turn >= self._max_calls:
            return
        # A newer snapshot replaces an older pending one: only the latest
        # transcript is worth speculating on.
        self._pending = (text, key)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        while self._pending is not None:
            wait = max(0.0, self._debounce - (perf_counter() - self._last_started))
            if wait:
                await asyncio.sleep(wait)
            pending, self._pending = self._pending, None
            if pending is None or self._calls_this_turn >= self._max_calls:
                return
            transcript, key = pending
            self._last_started = perf_counter()
            self._calls_this_turn += 1
            self.stats.started += 1
            started = perf_counter()
            try:
                result = await self._infer(transcript, key)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - speculation must never break a turn
                logger.debug("speculative inference failed", exc_info=True)
                self.stats.discard("error")
                continue
            self.stats.completed += 1
            if result is None:
                self.stats.discard("empty")
                continue
            # A newer transcript arrived while this ran: it is already stale.
            if self._pending is not None:
                self.stats.discard("superseded")
                continue
            self._result, self._result_key = result, key
            self._emit(
                "anticipation_ready",
                latency_ms=round((perf_counter() - started) * 1000),
                words=len(transcript.split()),
            )

    # ------------------------------------------------------------------ take

    def take(self, final: PreviewKey) -> Any:
        """The preview for exactly this final state, or ``None``. Never awaits.

        A preview still in flight is not waited for: it is simply absent, and
        the caller runs the canonical inference now. That is the §7 rule, and
        making ``take`` synchronous is what keeps it true no matter who calls it.
        """
        rejection: PreviewRejected | None = validate_preview(
            preview=self._result, computed_for=self._result_key, final=final
        )
        if rejection is not None:
            if self._result is not None or rejection.reason != "no_preview":
                self.stats.discard(rejection.reason)
                self._emit(
                    "anticipation_discarded",
                    reason=rejection.reason,
                    fields=",".join(rejection.fields),
                )
            return None
        self.stats.reused += 1
        self._emit("anticipation_reused")
        return self._result

    def _emit(self, event: str, **fields: Any) -> None:
        if self._on_event is None:
            return
        with contextlib.suppress(Exception):
            self._on_event(event, **fields)

    async def aclose(self) -> None:
        """Cancel outstanding speculation and wait for it to unwind."""
        task = self._task
        self._cancel()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
