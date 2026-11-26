"""Backchannel coordination in the voice runtime: when the Talker is asked, what is played.

Brief 2026-11-26 ("Context-Aware Streaming Backchanneling"). The Talker
(``agents/interviewer/talker.py``, the Luna model on the existing provider)
decides *whether* to speak and *what* to say; this module decides only
*when it is asked* and *whether its answer may still be played*. It holds no
templates and writes no words. It is the same discipline as the
Anticipation Engine applied to speech instead of decisions:

* **one inference in flight per session** -- a second request while one runs
  is not queued, it is skipped;
* **bounded calls**: at most ``calls_per_turn`` per candidate turn, and a
  minimum gap between two calls while listening. These are cost bounds, not
  pacing rules: the model is told how long ago it last spoke and chooses
  silence itself;
* **stale outputs are discarded**: a unit that arrives after the candidate
  started a new answer, after the authoritative reply is ready, or while the
  candidate is still talking (after a short hold), is dropped and counted;
* **never on the critical path**: ``observe`` returns at once; nothing here
  is awaited by the turn, and the Thinker's reply always wins (the runtime
  cuts a backchannel still playing when the reply arrives).

Pure asyncio; no LiveKit import, so it is unit-tested without a room.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from time import perf_counter
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["BackchannelCoordinator", "BackchannelStats"]

PHASE_LISTENING = "listening"
PHASE_PROCESSING = "processing"

#: How long a ready unit waits for the candidate to pause before it is dropped.
#: Long enough for a line prepared *during* speech to be played at the next
#: natural pause (audit 2026-11-26: generation is separate from playback).
HOLD_SECONDS = 6.0
#: How often the hold re-checks the candidate's state.
_HOLD_TICK = 0.1
#: A prepared line is stale when the candidate has said this many more words
#: since the transcript it was written for (revalidation against the current
#: context before playback). A scheduling constraint, not a content rule.
MAX_DRIFT_WORDS = 12
#: While the candidate is still speaking, ask again only after this many new words.
MIN_NEW_WORDS_WHILE_SPEAKING = 8
#: A prepared backchannel (text validated, audio synthesized) is kept this long
#: waiting for a pause; after that it is wasted synthesis, not a line to say.
PENDING_LIFETIME_SECONDS = 20.0

Infer = Callable[[dict[str, Any]], AsyncIterator[dict[str, Any]]]
Play = Callable[[str, str], Any]
#: ``await prepare(text) -> bool``: synthesize the validated line into the
#: audio cache now, so playing it later costs no TTS round trip.
Prepare = Callable[[str], Awaitable[bool]]


class BackchannelStats:
    __slots__ = ("requested", "skipped", "completed", "spoken", "discarded", "silent", "errors", "reasons", "overlaps",
                 "prepared", "spoken_prepared", "wasted", "speech_ends", "speech_ends_with_audio")

    def __init__(self) -> None:
        self.requested = 0
        self.skipped = 0
        self.completed = 0
        self.spoken = 0
        self.discarded = 0
        self.silent = 0
        self.errors = 0
        self.overlaps = 0
        self.prepared = 0
        self.spoken_prepared = 0
        self.wasted = 0
        self.speech_ends = 0
        self.speech_ends_with_audio = 0
        self.reasons: dict[str, int] = {}

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        judged = self.spoken + self.discarded
        return {
            "requested": self.requested, "skipped": self.skipped, "completed": self.completed,
            "spoken": self.spoken, "discarded": self.discarded, "silent": self.silent, "errors": self.errors,
            "overlaps": self.overlaps,
            "stale_rate": round(self.discarded / judged, 3) if judged else None,
            "prepared": self.prepared, "spoken_prepared": self.spoken_prepared, "wasted": self.wasted,
            "speech_ends": self.speech_ends, "speech_ends_with_audio": self.speech_ends_with_audio,
            "reasons": dict(self.reasons),
        }


class BackchannelCoordinator:
    """Asks the Talker at safe moments and plays what still fits."""

    def __init__(
        self,
        infer: Infer,
        play: Play,
        *,
        enabled: bool = False,
        calls_per_turn: int = 3,
        min_gap_seconds: float = 4.0,
        hold_seconds: float = HOLD_SECONDS,
        on_event: Callable[..., None] | None = None,
        prepare: Prepare | None = None,
        pending_lifetime_seconds: float = PENDING_LIFETIME_SECONDS,
    ) -> None:
        self._infer = infer
        self._play = play
        self._prepare = prepare
        self._lifetime = max(1.0, float(pending_lifetime_seconds))
        # The one prepared backchannel waiting for a pause (text, audio in the
        # cache, the transcript it was written for). Bounded: one item, one
        # short line, one lifetime.
        self._pending: dict[str, Any] | None = None
        self.enabled = enabled
        self._calls_per_turn = max(1, int(calls_per_turn))
        self._min_gap = max(0.0, float(min_gap_seconds))
        self._hold = max(0.0, float(hold_seconds))
        self._on_event = on_event
        self._task: asyncio.Task | None = None
        self._closing: set[asyncio.Task] = set()
        self._current_transcript = ""
        self._generation = 0
        self._calls_this_turn = 0
        self._last_started: float | None = None
        self._last_spoken_at: float | None = None
        self._last_transcript = ""
        self._speaking = False
        self._reply_ready = False
        self._playing: Any = None
        self._said: deque[str] = deque(maxlen=6)
        self.stats = BackchannelStats()

    # ----------------------------------------------------------------- state

    @property
    def in_flight(self) -> bool:
        return self._task is not None and not self._task.done()

    def reset_turn(self) -> None:
        """A new answer is starting: forget the previous turn's backchannels."""
        self._cancel("new_turn")
        self._drop_pending("new_turn")
        self._generation += 1
        self._calls_this_turn = 0
        self._last_transcript = ""
        self._reply_ready = False
        self._said.clear()

    def on_user_speaking(self) -> None:
        self._speaking = True
        playing = self._playing
        if playing is not None and not _done(playing):
            # The candidate started talking over a backchannel: the proxy for
            # "the interviewer interrupted the candidate" the benchmark reads.
            self.stats.overlaps += 1
            self._emit("backchannel_overlap")

    def on_user_stopped(self) -> None:
        """VAD end of speech: the moment a prepared backchannel is played.

        Not Qwen's final, not LiveKit's turn end, not the window's commit, not
        the Thinker: the VAD signal alone. A short pause is *not* taken as the
        end of the answer -- the line is an acknowledgment, the answer window
        and the Thinker go on exactly as before.
        """
        self._speaking = False
        if self.enabled:
            self.stats.speech_ends += 1
            available = self._pending is not None and bool(self._pending.get("audio_ready"))
            if available:
                self.stats.speech_ends_with_audio += 1
            self._emit("backchannel_speech_end", prepared_available=available, pending=self._pending is not None)
        self._play_pending(trigger="vad_stop")

    def on_reply_ready(self) -> None:
        """The authoritative reply exists: nothing pending may be played after it."""
        self._reply_ready = True
        self._cancel("reply_ready")
        self._drop_pending("reply_ready")

    def _drop_pending(self, reason: str) -> None:
        pending, self._pending = self._pending, None
        if pending is None:
            return
        self.stats.discarded += 1
        self.stats.note(reason)
        if pending.get("audio_ready"):
            self.stats.wasted += 1
        self._emit("backchannel_discarded", reason=reason, kind=pending.get("kind"), held_ms=None,
                   prepared_audio=bool(pending.get("audio_ready")))

    def _play_pending(self, *, trigger: str) -> bool:
        """Play the prepared line now if it still fits; otherwise drop it and say why."""
        pending = self._pending
        if pending is None:
            return False
        if self._speaking:
            return False
        if pending["generation"] != self._generation:
            self._drop_pending("new_turn")
            return False
        if self._reply_ready:
            self._drop_pending("reply_ready")
            return False
        if perf_counter() - pending["ready_at"] > self._lifetime:
            self._drop_pending("expired")
            return False
        drift = len(self._current_transcript.split()) - int(pending.get("prepared_for") or 0)
        if pending.get("prepared_for") and drift > MAX_DRIFT_WORDS:
            self._drop_pending("context_moved")
            return False
        self._pending = None
        self._speak(pending["unit"], pending["phase"], held=perf_counter() - pending["ready_at"],
                    prepared_audio=bool(pending.get("audio_ready")), trigger=trigger,
                    prepare_ms=pending.get("prepare_ms"))
        return True

    def _speak(self, unit: dict[str, Any], phase: str, *, held: float, prepared_audio: bool, trigger: str,
               prepare_ms: int | None = None) -> None:
        text = str(unit.get("text") or "").strip()
        self._playing = self._play(text, str(unit.get("kind") or "contextual"))
        logger.info("backchannel spoken", extra={"phase": phase, "kind": unit.get("kind"), "words": len(text.split()),
                                                 "held_ms": round(held * 1000), "prepared_audio": prepared_audio,
                                                 "trigger": trigger})
        self._last_spoken_at = perf_counter()
        self._said.append(text)
        self.stats.spoken += 1
        if prepared_audio:
            self.stats.spoken_prepared += 1
        self._emit(
            "backchannel_spoken",
            phase=phase,
            kind=unit.get("kind"),
            words=len(text.split()),
            ttft_ms=unit.get("ttft_ms"),
            ready_ms=unit.get("at_ms"),
            held_ms=round(held * 1000),
            prepared_audio=prepared_audio,
            trigger=trigger,
            prepare_ms=prepare_ms,
        )

    def _cancel(self, reason: str) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            # Kept until it has unwound: the next inference waits for it, so a
            # cancelled call never overlaps a new one (audit 2026-11-26).
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)
            self.stats.discarded += 1
            self.stats.note(f"cancelled:{reason}")
            logger.info("backchannel inference cancelled", extra={"reason": reason})
            # Persisted too: the benchmark showed 7 of 16 inferences ending
            # here with no event, invisible to the report.
            self._emit("backchannel_discarded", reason=f"cancelled:{reason}", kind=None, held_ms=None)

    async def _drain_closing(self) -> None:
        closing = [t for t in self._closing if not t.done()]
        if closing:
            await asyncio.gather(*closing, return_exceptions=True)

    def note_transcript(self, transcript: str) -> None:
        """The latest transcript (interim or final): what a held unit is checked against."""
        self._current_transcript = " ".join(str(transcript or "").split())

    # --------------------------------------------------------------- observe

    def observe(self, transcript: str, *, phase: str, answer_seconds: float | None = None, fragments: int = 0) -> bool:
        """A safe moment to ask: a fragment just ended, or the answer was submitted.

        Returns whether a request was started. Never awaits.
        """
        if not self.enabled:
            return False
        text = " ".join(str(transcript or "").split())
        now = perf_counter()
        self.stats.requested += 1
        if phase == PHASE_LISTENING and self._reply_ready:
            return self._skip("reply_ready")
        if self.in_flight:
            return self._skip("in_flight")
        if self._calls_this_turn >= self._calls_per_turn:
            return self._skip("turn_cap")
        if phase == PHASE_LISTENING:
            if not text:
                return self._skip("empty")
            if text == self._last_transcript:
                return self._skip("unchanged")
            if self._last_started is not None and now - self._last_started < self._min_gap:
                return self._skip("min_gap")
            if self._speaking and len(text.split()) - len(self._last_transcript.split()) < MIN_NEW_WORDS_WHILE_SPEAKING:
                # Preparing during speech: only once the candidate has said
                # enough new words for the model to have something new to follow.
                return self._skip("few_new_words")
        self._current_transcript = text
        self._last_transcript = text
        self._last_started = now
        self._calls_this_turn += 1
        request = {
            "transcript": text,
            "phase": phase,
            "speaking": self._speaking,
            "prepared_for_words": len(text.split()),
            "signals": {
                "seconds_since_last_backchannel": round(now - self._last_spoken_at, 1) if self._last_spoken_at else None,
                "answer_seconds": round(answer_seconds, 1) if answer_seconds is not None else None,
                "fragments": int(fragments),
                "last_backchannels": list(self._said),
            },
        }
        self._task = asyncio.create_task(self._run(request, self._generation))
        return True

    def _skip(self, reason: str) -> bool:
        self.stats.skipped += 1
        self.stats.note(f"skipped:{reason}")
        return False

    # ------------------------------------------------------------------- run

    async def _run(self, request: dict[str, Any], generation: int) -> None:
        started = perf_counter()
        phase = request["phase"]
        done: dict[str, Any] = {}
        spoken = 0
        try:
            # A cancelled inference still unwinding must finish before this
            # one starts: never two Talker streams for one session.
            await self._drain_closing()
            if generation != self._generation:
                return
            inferring = self._infer(request)
            try:
                async for item in inferring:
                    kind = item.get("type")
                    if kind == "done":
                        done = item
                        break
                    if kind != "unit":
                        continue
                    if await self._deliver(item, generation, phase, prepared_for=int(request.get("prepared_for_words") or 0)):
                        spoken += 1
            finally:
                # Leaving the loop early (done, cancelled) must close the
                # API stream too, which closes the provider's (audit item 6).
                aclose = getattr(inferring, "aclose", None)
                if callable(aclose):
                    with contextlib.suppress(Exception):
                        await aclose()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a backchannel must never cost the candidate anything
            self.stats.errors += 1
            self._emit("backchannel", phase=phase, kind=None, ok=False, error_type=type(exc).__name__,
                       total_ms=round((perf_counter() - started) * 1000))
            logger.debug("backchannel inference failed", exc_info=True)
            return
        self.stats.completed += 1
        if done.get("kind") == "silence":
            self.stats.silent += 1
        self._emit(
            "backchannel",
            phase=phase,
            kind=done.get("kind"),
            ok=done.get("reason") is None,
            reason=done.get("reason"),
            ttft_ms=done.get("ttft_ms"),
            total_ms=done.get("total_ms", round((perf_counter() - started) * 1000)),
            units=done.get("units", 0),
            rejected=len(done.get("rejected") or []),
            spoken=spoken,
        )

    async def _deliver(self, unit: dict[str, Any], generation: int, phase: str, *, prepared_for: int = 0) -> bool:
        text = str(unit.get("text") or "").strip()
        if not text:
            return False
        if generation != self._generation:
            return self._discard("new_turn", unit, 0.0)
        if self._reply_ready:
            return self._discard("reply_ready", unit, 0.0)
        if not self._speaking:
            # A pause is already here: say it now (TTS on the critical path of
            # this line only, never of the turn).
            drift = len(self._current_transcript.split()) - prepared_for
            if prepared_for and drift > MAX_DRIFT_WORDS:
                return self._discard("context_moved", unit, 0.0)
            self._speak(unit, phase, held=0.0, prepared_audio=False, trigger="ready")
            return True
        # The candidate is still talking: prepare the audio *now* and keep the
        # line for the VAD end of speech (on_user_stopped). One pending item;
        # a newer one replaces an older one that never got its pause.
        if self._pending is not None:
            self._drop_pending("superseded")
        started = perf_counter()
        audio_ready = False
        if self._prepare is not None:
            try:
                audio_ready = bool(await self._prepare(text))
            except Exception:  # noqa: BLE001 - no audio means the line is synthesized at playback
                logger.debug("backchannel audio preparation failed", exc_info=True)
        prepare_ms = round((perf_counter() - started) * 1000)
        if generation != self._generation:
            return self._discard("new_turn", unit, 0.0)
        if self._reply_ready:
            return self._discard("reply_ready", unit, 0.0)
        self.stats.prepared += 1
        self._pending = {
            "unit": unit, "text": text, "kind": unit.get("kind"), "phase": phase, "generation": generation,
            "prepared_for": prepared_for, "ready_at": perf_counter(), "audio_ready": audio_ready, "prepare_ms": prepare_ms,
        }
        self._emit("backchannel_prepared", phase=phase, kind=unit.get("kind"), words=len(text.split()),
                   audio_ready=audio_ready, prepare_ms=prepare_ms, ttft_ms=unit.get("ttft_ms"))
        # The candidate may have paused during synthesis.
        return self._play_pending(trigger="ready")

    def _discard(self, reason: str, unit: dict[str, Any], held: float) -> bool:
        logger.info("backchannel discarded", extra={"reason": reason, "kind": unit.get("kind"), "held_ms": round(held * 1000)})
        self.stats.discarded += 1
        self.stats.note(reason)
        self._emit("backchannel_discarded", reason=reason, kind=unit.get("kind"), held_ms=round(held * 1000))
        return False

    def _emit(self, event: str, **fields: Any) -> None:
        if self._on_event is None:
            return
        with contextlib.suppress(Exception):
            self._on_event(event, **fields)

    async def aclose(self) -> None:
        task = self._task
        self._cancel("close")
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def _done(handle: Any) -> bool:
    done = getattr(handle, "done", None)
    try:
        return bool(done()) if callable(done) else True
    except Exception:  # noqa: BLE001
        return True
