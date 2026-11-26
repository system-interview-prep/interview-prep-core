"""LiveKit voice adapter for the authenticated Interview Core runtime.

The API lifespan starts this worker automatically when ``LIVEKIT_AGENT_AUTOSTART``
is enabled. It keeps streaming speech recognition and ElevenLabs TTS (with a
LiveKit Inference fallback voice), while
Interview Core supplies every interview question and response.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from hashlib import sha256
from pathlib import Path
from time import perf_counter

import httpx
from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentServer, AgentSession, inference
from livekit.agents.voice.turn import InterruptionOptions
from livekit.plugins import elevenlabs

from src.core.config import get_settings
from src.modules.interviews.realtime.anticipation import AnticipationEngine, PreviewKey, preview_key
from src.modules.interviews.realtime.backchannel import (
    PHASE_LISTENING, PHASE_PROCESSING, BackchannelCoordinator, stable_prefix,
)
from src.modules.interviews.realtime.endpointing import (
    ANSWER_PAUSE_NUDGE_SECONDS, PROCESSING_FILLER_SECONDS, AnswerWindow,
    answer_pause_nudge_enabled, answer_pause_nudges,
    backchannel_continue_text, commit_anchor, core_error_text, localize_particles, not_heard_lines,
    pick_answer_pause_nudge, pick_processing_filler,
    processing_filler_enabled, processing_fillers, remaining_commit_delay, reprompt_after_seconds,
    silence_reprompt, turn_taking_tip, unclear_transcript_text,
)
from src.modules.interviews.realtime.health import (
    provider_error_payload,
    provider_recovered_payload,
)
from src.modules.interviews.realtime.interruption import (
    capture_interruption, classify_interruption, hold_acknowledgement, is_greeting_backchannel,
    is_repeat_request, is_resume_request, remaining_after_interruption, resolve_interruption,
)
from src.modules.interviews.realtime.events import VoiceEventReporter
from src.modules.interviews.realtime.config.tuning import voice_tuning
from src.modules.interviews.realtime.speech.language import (
    normalize_transcript_for_locale,
    speakable_text,
)
from src.modules.interviews.realtime.stt.recognition import (
    create_stt,
    recognition_language,
    recognition_options,
    recognition_room_options,
)
from src.modules.interviews.realtime.stt.provider import stt_provider_settings
from src.modules.interviews.realtime.stt.transcript_quality import process_transcript
from src.modules.interviews.realtime.tts.provider import tts_provider_config
from src.modules.interviews.realtime.tts.cache import GrowingClip, SpeechAudioCache, SpeechClip
from src.modules.interviews.realtime.tts.vieneu import VieNeuTTS

def _project_root() -> Path:
    """The directory that holds ``src/``, found by walking up from this file.

    A fixed ``parents[N]`` broke when this module moved from
    ``adapters/voice/runtime/`` to ``realtime/runtime/`` in the V2 package
    migration: the index still resolved, one directory too high, so the worker
    would have started with no ``.env`` and no error. Walking up is immune to
    the next move.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "src").is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    return here.parents[5]


# Prefer variables supplied by Docker/the backend environment. The project-root
# .env file only fills values that are not already present.
load_dotenv(_project_root() / ".env", override=False)

# The ElevenLabs plugin uses ELEVEN_API_KEY, while the existing Voice Lab uses
# the more explicit ELEVENLABS_API_KEY name.
if os.getenv("ELEVENLABS_API_KEY") and not os.getenv("ELEVEN_API_KEY"):
    os.environ["ELEVEN_API_KEY"] = os.environ["ELEVENLABS_API_KEY"]


AGENT_INSTRUCTIONS = """
You are the voice transport for an interview controlled by Interview Core.
Interview Core supplies every greeting, question, probe, clarification, and closing.
Never create, rephrase, or add interview content independently.
"""

logger = logging.getLogger(__name__)


def _float_env(name: str, default: float) -> float:
    """Read a numeric tuning value without making a bad .env stop the agent."""
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    """Read a non-negative integer without making a bad .env stop the agent."""
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _session_stt_language(locale: str) -> str:
    """Keep recognition in the interview language for the whole session."""
    return recognition_language(locale)


#: While Core thinks, the TTS connection is pinged this often so the reply's
#: first byte is not a cold start (tts/vieneu.py ``warm``). Measured
#: 2026-11-26: warm at 4 s idle (0.46 s), cold at 6 s (1.07 s).
TTS_KEEPALIVE_SECONDS = 3.0


def _delta_ms(start: float | None, end: float | None) -> int | None:
    if start is None or end is None:
        return None
    return round((end - start) * 1000)


def _message_metrics(message: object) -> dict[str, float]:
    raw = getattr(message, "metrics", None)
    if not isinstance(raw, dict):
        return {}
    names = (
        "started_speaking_at",
        "stopped_speaking_at",
        "transcription_delay",
        "end_of_turn_delay",
        "on_user_turn_completed_delay",
        "e2e_latency",
    )
    return {name: round(float(raw[name]), 4) for name in names if isinstance(raw.get(name), (int, float))}


def opening_paragraphs(text: str) -> list[str]:
    """The opening's blank-line paragraphs (core/dialogue/opening.py), in order."""
    paragraphs = [part.strip() for part in text.split("\n\n")]
    return [part for part in paragraphs if part] or [text]


class VoiceLabAgent(Agent):
    def __init__(
        self,
        *,
        runtime_client: httpx.AsyncClient,
        session_id: str,
        room_name: str,
        room: object,
        locale: str,
        session_context: str = "",
        stt_provider: str = "assemblyai",
        enable_speculative_preview: bool = False,
        speech_cache: SpeechAudioCache | None = None,
        on_interview_closed=None,
        events: VoiceEventReporter | None = None,
        warm_tts=None,
        enable_backchannel: bool = False,
        backchannel_calls_per_turn: int = 3,
        backchannel_min_gap_seconds: float = 4.0,
        prepare_tts=None,
    ) -> None:
        super().__init__(instructions=AGENT_INSTRUCTIONS)
        # The TTS used to synthesize a validated backchannel *while the
        # candidate speaks*, into the speech cache, so playing it at the VAD
        # end of speech is a cache hit (tts_node). None: synthesize at playback.
        self._prepare_tts = prepare_tts
        # ``await warm_tts()`` keeps the TTS connection open (VieNeu); None for
        # providers that do not need it.
        self._warm_tts = warm_tts
        # Voice-room state and incidents for the admin view (voice_events.py).
        self._events = events
        # Called once Core has closed the interview, to release the session.
        self._on_interview_closed = on_interview_closed
        self._speech_cache = speech_cache
        # Turned off while the primary voice is down (see tts_availability_changed).
        self.cache_writes_enabled = True
        self._runtime_client = runtime_client
        self._session_id = session_id
        self._room_name = room_name
        self._room = room
        self._locale = locale
        self._session_context = session_context.strip()
        self._stt_provider = stt_provider
        self._enable_speculative_preview = enable_speculative_preview
        self._active_speech_handle: object | None = None
        self._active_speech_text = ""
        self._active_speech_kind = "question"
        self._last_answer_metrics: dict[str, float] = {}
        # Turn clock for the §13 metrics (TTFA, TTMR): when the candidate's
        # speech ended, when the answer was submitted, when Core answered.
        # Read by on_agent_speaking() when the next utterance starts playing.
        self._speech_ended_at: float | None = None
        self._submitted_at: float | None = None
        self._core_completed_at: float | None = None
        # The endpointing chain of the current answer (critical-path profile):
        # the last final transcript, LiveKit's turn end, the wait the window
        # scheduled. Reported once per submit as ``voice_endpointing``.
        self._last_final_at: float | None = None
        self._turn_completed_at: float | None = None
        self._commit_delay_s: float | None = None
        self._commit_effective_s: float | None = None
        # The nudge or filler being played, so the real reply can cut it.
        self._filler_handle: object | None = None
        # Deterministic warm-up of the next turn (no provider call), once per answer.
        self._prefetch_task: asyncio.Task | None = None
        self._prefetched_generation = -1
        self._tts_keepalive_task: asyncio.Task | None = None
        self._last_interview_speech_text = ""
        self._held_speech_text = ""
        self._held_resume_text = ""
        # Answer window: fragments of one spoken answer, submitted together.
        self._answer = AnswerWindow()
        self._commit_task: asyncio.Task | None = None
        self._answer_pause_nudge_task: asyncio.Task | None = None
        self._answer_pause_nudged = False
        self._answer_pause_nudge_count = 0
        self._last_answer_pause_nudge: str | None = None
        self._core_task: asyncio.Task | None = None
        # Speculation is owned by the Anticipation Engine, not by this class
        # (V2 brief §5). The runtime supplies the inference and the key; the
        # engine owns the task, the budget and the staleness rules, and its
        # `take` is synchronous so no code here can wait on a preview.
        self._turn_generation = 0
        # Hybrid (V2 §6.4): consume the API's early acknowledgment stream.
        # Off by default; read once so delivery cannot change mid-session.
        self._hybrid_enabled = bool(get_settings().interview_talker_enabled)
        self._anticipation = AnticipationEngine(
            self._speculate,
            enabled=enable_speculative_preview,
            # The engine names its events generically; report() drops anything
            # outside VOICE_EVENTS, so they are prefixed here to be kept.
            on_event=lambda event, **fields: self.report(f"voice_{event}", **fields),
        )
        # Context-aware backchanneling (brief 2026-11-26): the Talker decides
        # to speak or not; the coordinator decides when it is asked and
        # whether its answer may still be played. When it is on, the fixed
        # nudge and processing-filler lines are not used: they are templates.
        self._backchannel = BackchannelCoordinator(
            self._backchannel_infer,
            self._speak_backchannel,
            enabled=enable_backchannel,
            calls_per_turn=backchannel_calls_per_turn,
            min_gap_seconds=backchannel_min_gap_seconds,
            on_event=lambda event, **fields: self.report(f"voice_{event}", **fields),
            prepare=self._prepare_backchannel_audio,
        )
        self._core_in_flight = False
        self._repeat_after_core = False
        self._reprompt_task: asyncio.Task | None = None
        self._reprompted_text = ""
        self._last_interruption_telemetry: dict = {}

    # ── Admin view ───────────────────────────────────────────────────────────

    def report(self, event: str, **fields) -> None:
        if self._events is not None:
            self._events.emit(event, **fields)

    def report_state(self, state: str, **fields) -> None:
        if self._events is not None:
            self._events.state(state, **fields)

    def _resting_state(self) -> str:
        """Where the turn is when nobody is speaking."""
        if self._core_in_flight:
            return "processing"
        if not self._answer.is_empty:
            return "answer_pending"
        return "waiting_answer"

    def _ms_since(self, mark: str) -> int | None:
        started = getattr(self, mark, None)
        return None if started is None else round((perf_counter() - started) * 1000)

    def on_agent_speaking(self) -> None:
        self.report_state("ai_speaking", kind=self._active_speech_kind)
        # V2 spec §13: TTFA is speech end → first audio, TTMR is speech end →
        # the official reply. Both are read off this event by kind
        # (early_ack / filler count for TTFA only; question / closing for
        # both) in scripts/eval/interviews/voice/livekit_turn_metrics.py.
        # Measured at the agent, when LiveKit starts playing the utterance;
        # client playback adds the network leg this process cannot see.
        self.report(
            "voice_reply_started",
            kind=self._active_speech_kind,
            since_speech_end_ms=self._ms_since("_speech_ended_at"),
            since_submit_ms=self._ms_since("_submitted_at"),
            since_core_ms=self._ms_since("_core_completed_at"),
        )

    def _bc(self) -> BackchannelCoordinator:
        """The coordinator, or a disabled one on an instance built without __init__ (tests)."""
        coordinator = getattr(self, "_backchannel", None)
        if coordinator is None:
            coordinator = self._backchannel = BackchannelCoordinator(self._backchannel_infer, self._speak_backchannel, enabled=False)
        return coordinator

    def on_user_stopped(self) -> None:
        self._speech_ended_at = perf_counter()
        self._bc().on_user_stopped()
        self.report_state(self._resting_state())

    # ── Backchanneling ───────────────────────────────────────────────────────

    async def _backchannel_infer(self, request: dict):
        """The Talker's streamed units, from the API (NDJSON). Read-only; writes no state."""
        async with self._runtime_client.stream(
            "POST", "/ai/voice-lab/livekit/runtime/backchannel", json=request
        ) as response:
            response.raise_for_status()
            async for raw in response.aiter_lines():
                if raw.strip():
                    yield json.loads(raw)

    async def _prepare_backchannel_audio(self, text: str) -> bool:
        """Synthesize a validated backchannel into the speech cache, progressively.

        Same adapter, same voice, same cache ``tts_node`` reads: the later
        ``speak()`` of this exact text replays the clip instead of calling
        VieNeu. In memory only (never on disk: the line is this answer's).

        Progressive (brief 2026-11-26): a :class:`GrowingClip` goes into the
        cache as soon as the **first PCM** arrives (~0.45 s on a warm
        connection) and this returns True then; the rest of the synthesis
        continues in its own task and keeps appending, also while the clip is
        already playing. Readiness therefore no longer waits for the whole
        line (2-3 s). Returns False when there is nothing to prepare with or
        no audio came, so the caller synthesizes at playback instead.
        """
        cache = getattr(self, "_speech_cache", None)
        tts = getattr(self, "_prepare_tts", None)
        if cache is None or tts is None:
            return False
        key = localize_particles(text)
        if cache.get(key) is not None:
            return True
        stream = tts.synthesize(speakable_text(key, locale=self._locale))
        started = perf_counter()
        clip: GrowingClip | None = None
        first_pcm = asyncio.get_running_loop().create_future()

        async def pump() -> None:
            nonlocal clip
            try:
                async for event in stream:
                    frame = getattr(event, "frame", None)
                    if frame is None:
                        continue
                    if clip is None:
                        clip = GrowingClip(sample_rate=frame.sample_rate, channels=frame.num_channels)
                        cache.put(key, clip, persist=False)
                    clip.append(bytes(frame.data), now=perf_counter())
                    if not first_pcm.done():
                        first_pcm.set_result(True)
            except Exception:  # noqa: BLE001 - a failed prepare means synthesis at playback
                logger.debug("progressive backchannel synthesis failed", exc_info=True)
                if clip is not None:
                    clip.finish(failed=True, now=perf_counter())
            finally:
                aclose = getattr(stream, "aclose", None)
                if callable(aclose):
                    with contextlib.suppress(Exception):
                        await aclose()
                if clip is not None and not clip.finished:
                    clip.finish(now=perf_counter())
                    with contextlib.suppress(Exception):
                        cache.put(key, clip.to_clip(), persist=False)
                    self.report(
                        "voice_backchannel_synthesized",
                        first_pcm_ms=round(((clip.first_pcm_at or started) - started) * 1000),
                        total_ms=round((perf_counter() - started) * 1000),
                        audio_ms=clip.audio_ms,
                    )
                if not first_pcm.done():
                    first_pcm.set_result(False)

        self._prepare_task = asyncio.create_task(pump())
        return bool(await first_pcm)

    def _speak_backchannel(self, text: str, kind: str):
        """Play one validated unit: interruptible, outside the chat context,
        and cut by the authoritative reply like any filler (``_interrupt_filler``)."""
        handle = self.speak(text, allow_interruptions=True, add_to_chat_ctx=False, kind="backchannel")
        self._filler_handle = handle
        return handle

    def on_final_transcript(self) -> None:
        """A final transcript arrived from STT (before LiveKit ends the turn)."""
        self._last_final_at = perf_counter()

    def on_user_away(self) -> None:
        self.report_state("candidate_away")

    # ── Answer window ────────────────────────────────────────────────────────

    def _publish_turn_state(self, state: str) -> None:
        """Tell the room UI where the answer is: answering | processing | listening."""
        self.report_state(
            {"answering": "answer_pending", "processing": "processing"}.get(state, "waiting_answer")
        )
        payload = json.dumps({"type": "turn.state", "state": state, "sessionId": self._session_id})
        with contextlib.suppress(Exception):
            asyncio.create_task(
                self._room.local_participant.publish_data(payload, reliable=True, topic="intervia.turn")
            )

    def _cancel_task(self, task: asyncio.Task | None) -> None:
        if task is not None and not task.done():
            task.cancel()

    def _schedule_commit(self, delay: float) -> None:
        self._cancel_task(self._commit_task)
        # ``delay`` is the window's silence allowance; where that silence is
        # counted from is the anchor's business (endpointing.py).
        effective = remaining_commit_delay(
            delay, speech_ended_at=getattr(self, "_speech_ended_at", None), now=perf_counter(), anchor=commit_anchor()
        )
        self._commit_delay_s, self._commit_effective_s = delay, effective
        self._commit_task = asyncio.create_task(self._commit_after(effective))

    async def _commit_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        # The nudge is optional ambience, not part of the answer. Do not make
        # the candidate wait for its TTS playout before Core can start.
        self._cancel_task(self._answer_pause_nudge_task)
        # The Core call runs in its own task. `_commit_task` is cancelled
        # whenever the candidate speaks; if the call ran inside it, talking
        # while Core works ("alo?") would cancel the request mid-flight and
        # leave `_core_in_flight` set, so every later answer was ignored.
        self._core_task = asyncio.create_task(self._commit_answer())

    def on_user_speaking(self) -> None:
        """The candidate is (still) talking: the answer is not finished yet."""
        self._cancel_task(self._commit_task)
        self._cancel_task(self._answer_pause_nudge_task)
        self._cancel_task(self._reprompt_task)
        self._bc().on_user_speaking()
        if self._answer.is_empty:
            # A new answer is starting: drop the previous turn's speculation
            # and give this turn a fresh budget.
            self._turn_generation += 1
            self._anticipation.reset_turn()
            self._bc().reset_turn()
        self._start_prefetch()
        # With backchanneling on, a prepared line is synthesized *during* the
        # answer: keep the TTS connection warm from the first word, not only
        # while Core thinks (measured: 455 ms warm vs 1 126 ms cold first byte).
        if self._bc().enabled and getattr(self, "_warm_tts", None) is not None:
            task = getattr(self, "_tts_keepalive_task", None)
            if task is None or task.done():
                self._tts_keepalive_task = asyncio.create_task(self._tts_keepalive())

    # ── Deterministic warm-up while the candidate speaks ─────────────────────

    def _start_prefetch(self) -> None:
        """Warm what the next turn will read, with no provider call, once per answer.

        Brief §4: the deterministic part of the turn -- candidate memory, the
        interviewer playbook, the TTS connection -- is prepared while the
        candidate is still talking, and only that part; the endpoint runs
        under a zero call budget, so a provider call there is an error, not
        a cost. Best effort, never awaited by the turn, and the authoritative
        turn state is still read under the claim's row lock when the answer
        is submitted.
        """
        if self._core_in_flight or getattr(self, "_prefetched_generation", -1) == self._turn_generation:
            return
        self._prefetched_generation = self._turn_generation
        self._cancel_task(getattr(self, "_prefetch_task", None))
        self._prefetch_task = asyncio.create_task(self._prefetch())

    async def _prefetch(self) -> None:
        started = perf_counter()
        warm_tts = getattr(self, "_warm_tts", None)
        warm = warm_tts() if warm_tts is not None else None
        try:
            response = await self._runtime_client.post("/ai/voice-lab/livekit/runtime/prefetch", json={})
            response.raise_for_status()
            body = response.json() if hasattr(response, "json") else {}
            warmed = body.get("warmed") if isinstance(body, dict) else None
            self.report(
                "voice_prefetch",
                ok=True,
                ms=round((perf_counter() - started) * 1000),
                server_ms=body.get("ms") if isinstance(body, dict) else None,
                warmed=",".join(str(w) for w in (warmed or []))[:60] or None,
            )
        except Exception as exc:  # noqa: BLE001 - a warm-up must never cost the candidate anything
            self.report(
                "voice_prefetch", ok=False, ms=round((perf_counter() - started) * 1000),
                error_type=type(exc).__name__,
            )
        if warm is not None:
            with contextlib.suppress(Exception):
                await warm

    async def _tts_keepalive(self) -> None:
        """Ping the TTS connection every few seconds while Core thinks."""
        warm_tts = getattr(self, "_warm_tts", None)
        if warm_tts is None:
            return
        while True:
            with contextlib.suppress(Exception):
                await warm_tts()
            await asyncio.sleep(TTS_KEEPALIVE_SECONDS)

    def _schedule_answer_pause_nudge(self) -> None:
        if self._answer_pause_nudged or self._core_in_flight or self._answer.is_empty:
            return
        if not answer_pause_nudge_enabled() or self._bc().enabled:
            return
        self._cancel_task(self._answer_pause_nudge_task)
        # Same clock as the commit: a nudge is "you paused", so it is counted
        # from the same moment the pause is.
        nudge_delay = remaining_commit_delay(
            ANSWER_PAUSE_NUDGE_SECONDS, speech_ended_at=getattr(self, "_speech_ended_at", None),
            now=perf_counter(), anchor=commit_anchor(),
        )

        async def nudge_after_pause() -> None:
            try:
                await asyncio.sleep(nudge_delay)
            except asyncio.CancelledError:
                return
            if self._answer.is_empty or self._core_in_flight or self._answer_pause_nudged:
                return
            self._answer_pause_nudged = True
            self._answer_pause_nudge_count += 1
            line = pick_answer_pause_nudge(
                self._locale,
                f"{self._session_id}:answer-pause:{self._answer_pause_nudge_count}",
                self._last_answer_pause_nudge,
            )
            self._last_answer_pause_nudge = line
            self.report("voice_answer_pause_nudge", after_s=ANSWER_PAUSE_NUDGE_SECONDS)
            speech = self.speak(
                line,
                allow_interruptions=True,
                add_to_chat_ctx=False,
                kind="filler",
            )
            self._filler_handle = speech
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await speech.wait_for_playout()

        self._answer_pause_nudge_task = asyncio.create_task(nudge_after_pause())

    def on_interim_transcript(self, text: str) -> None:
        """Speculatively evaluate a stable rolling transcript without committing it."""
        # Endpointing needs to know whether the tail is still moving whether or
        # not speculation is on, so this is fed before the preview guard.
        self._answer.note_interim(text)
        # A stable partial (Qwen re-emitted the same interim: the candidate is
        # between phrases) is a safe moment to let the Talker *prepare* a line
        # for the next pause; the coordinator bounds how often, and playback
        # still waits for the pause (audit 2026-11-26, item 1-2).
        if self._bc().enabled:
            interim = " ".join(str(text or "").split())
            partial = " ".join(part for part in (self._answer.text, interim) if part)
            self._bc().note_transcript(partial)
            # What the Talker is given while the candidate talks is the
            # *stable prefix* of the interim -- the words two successive
            # interims agree on -- not the tail Qwen is still revising
            # (measured: the tail of 18 of 21 interims changes later).
            previous = getattr(self, "_previous_interim", "")
            self._previous_interim = interim
            stable = stable_prefix(previous, interim) if previous else ""
            partial = " ".join(part for part in (self._answer.text, stable) if part)
        # Run 6 (2026-11-26) measured the stable-partial trigger as a pause
        # trigger in disguise: Qwen revises the tail of 18 of 21 interims while
        # the candidate talks, so an interim repeats only in a pause -- and
        # then the VAD stops too, the line is written after the pause and the
        # audio arrives ~1.9 s later. Prepared audio was available at 1 of 16
        # speech ends. So a *growing* partial is also offered: the coordinator
        # asks only after enough new words (its cost bound), the model is told
        # the transcript is partial, and the line is re-checked for drift
        # before it is played.
        if self._bc().enabled and (self._answer.transcript_stable or self._bc().grew_enough(partial)):
            self._bc().observe(
                partial, phase=PHASE_LISTENING, answer_seconds=self._answer.duration_seconds,
                fragments=len(self._answer.fragments),
            )
        if not self._enable_speculative_preview:
            return
        transcript = normalize_transcript_for_locale(
            text,
            locale=self._locale,
            context=f"{getattr(self, '_session_context', '')} {self._last_interview_speech_text}".strip(),
            is_final=False,
        ).strip()
        combined = " ".join(part for part in (self._answer.text, transcript) if part)
        if len(combined) >= 16 and len(combined.split()) >= 4:
            self._schedule_preview(combined)

    def _preview_key(self, transcript: str) -> PreviewKey:
        """What this worker can truthfully say a speculation assumed.

        Only two things: the session, and the transcript it heard. The turn id,
        question version, session-state, rubric and policy versions are the
        server's to know; an earlier version of this method filled them with a
        local counter and a hash of the spoken prompt, which made the engine's
        comparison look seven-wide while checking nothing real. They are left
        ``None`` here, the engine reports them as unchecked, and the server
        recomputes and enforces all seven at redemption (lab.py).

        The turn generation still exists -- it resets the speculation budget and
        cancels the previous answer's task when a new answer starts -- it just
        no longer pretends to be a turn id.
        """
        return preview_key(session_id=self._session_id, transcript=transcript)

    async def _speculate(self, transcript: str, key: PreviewKey) -> str | None:
        """Ask the runtime for a preview token. Read-only; never writes state."""
        response = await self._runtime_client.post(
            "/ai/voice-lab/livekit/runtime/preview",
            json={"transcript": transcript},
        )
        response.raise_for_status()
        token = response.json().get("previewToken")
        return token if isinstance(token, str) and token else None

    def _schedule_preview(self, transcript: str) -> None:
        """Hand a partial transcript to the engine. Returns immediately."""
        if self._core_in_flight:
            return
        self._anticipation.observe(transcript, self._preview_key(transcript))
        if self._enable_speculative_preview:
            # Previously emitted at the end of the drain loop, which only ran
            # when speculation was on. Kept on that same condition.
            self.report_state("candidate_speaking")

    def on_agent_listening(self) -> None:
        """A question finished playing: start waiting for the answer."""
        self.report_state(self._resting_state())
        if not self._answer.is_empty or self._core_in_flight or not self._last_interview_speech_text:
            return
        if self._reprompted_text == self._last_interview_speech_text:
            return  # one gentle re-prompt per question
        self._cancel_task(self._reprompt_task)
        self._reprompt_task = asyncio.create_task(self._reprompt_after_silence())

    async def _reprompt_after_silence(self) -> None:
        try:
            await asyncio.sleep(reprompt_after_seconds())
        except asyncio.CancelledError:
            return
        if self._answer.is_empty and not self._core_in_flight:
            self._reprompted_text = self._last_interview_speech_text
            self.report("voice_silence_reprompt", after_s=reprompt_after_seconds())
            self.speak(
                silence_reprompt(self._locale),
                allow_interruptions=True,
                add_to_chat_ctx=False,
                kind="reprompt",
            )

    def handle_client_message(self, message: dict) -> None:
        """Data messages from the room UI (topic ``intervia.answer``)."""
        kind = message.get("type")
        if kind == "question.repeat" and self._last_interview_speech_text:
            self.report("voice_question_replayed", reason="button")
            self.speak(
                self._last_interview_speech_text,
                allow_interruptions=True,
                add_to_chat_ctx=False,
                kind="replay",
            )

    def _not_heard_lines(self) -> tuple[str, ...]:
        return not_heard_lines(self._locale)

    def _not_heard_text(self) -> str:
        """Rotates so a candidate asked to repeat twice hears two different lines."""
        lines = self._not_heard_lines()
        self._not_heard_count = getattr(self, "_not_heard_count", 0) + 1
        return lines[(self._not_heard_count - 1) % len(lines)]

    def _core_error_text(self) -> str:
        return core_error_text(self._locale)

    def _unclear_transcript_text(self) -> str:
        return unclear_transcript_text(self._locale)

    async def _submit_answer(self, answer, preview_token: str | None, core_started: float) -> dict:
        """POST the answer; on the Hybrid path, speak the early line as it arrives.

        V2 spec §6.4 and the correction brief §7. The API streams NDJSON when
        asked: zero or one ``response.early`` as soon as the Talker's guarded
        acknowledgment exists, then one ``response.final``. The early line is
        spoken immediately, interruptible, outside the chat context -- it is a
        beat, not a decision -- and the method returns only the final body, so
        everything after it is unchanged. Without Hybrid the plain JSON path is
        used, exactly as before.
        """
        body = {
            "clientMessageId": answer.client_message_id,
            "content": answer.text,
            "durationSeconds": answer.duration_seconds,
            "telemetry": {
                "roomName": self._room_name,
                "rawTranscript": answer.raw_text,
                "answerFragments": answer.fragment_count,
                "transcriptConfidence": answer.confidence,
                "interruption": self._last_interruption_telemetry,
            },
            "previewToken": preview_token,
        }
        if not getattr(self, "_hybrid_enabled", False):
            response = await self._runtime_client.post("/ai/voice-lab/livekit/runtime/message", json=body)
            response.raise_for_status()
            return response.json()

        final: dict | None = None
        async with self._runtime_client.stream(
            "POST", "/ai/voice-lab/livekit/runtime/message", params={"stream": "1"}, json=body
        ) as response:
            response.raise_for_status()
            async for raw in response.aiter_lines():
                if not raw.strip():
                    continue
                event = json.loads(raw)
                kind = event.get("type")
                if kind == "response.early":
                    text_value = str(event.get("text") or "").strip()
                    if text_value and not self._answer_pause_nudged:
                        self.report(
                            "voice_early_acknowledgment",
                            after_ms=round((perf_counter() - core_started) * 1000),
                        )
                        # Guarded server-side (acknowledgement guard in the
                        # Talker). Interruptible so a candidate who resumes
                        # talking cuts it off; never added to the chat context.
                        self.speak(text_value, allow_interruptions=True, add_to_chat_ctx=False, kind="early_ack")
                elif kind == "response.final":
                    final = {k: v for k, v in event.items() if k != "type"}
                elif kind == "response.error":
                    raise RuntimeError(
                        f"Interview Core returned {event.get('status')}: {event.get('detail')}"
                    )
        if final is None:
            raise RuntimeError("Interview Core stream ended without a final response")
        return final

    async def _commit_answer(self) -> None:
        """Submit the buffered answer to Interview Core and speak its reply."""
        if self._core_in_flight:
            return  # speech that arrives meanwhile waits for the next question
        answer_text = self._answer.text
        if not answer_text:
            return
        # V2 spec §7 / brief §5: speculation must never delay the canonical
        # inference. This used to schedule a fresh preview for the very text it
        # was about to submit, then hold the turn up to a second waiting for
        # it. Both are gone: a preview that is already finished and still
        # matches is reused below, and one still in flight is simply not there.
        answer = self._answer.take()
        if answer is None:
            return
        nudged = self._answer_pause_nudged
        self._answer_pause_nudged = False
        self._cancel_task(self._answer_pause_nudge_task)
        # Synchronous by contract: a preview still in flight is simply absent
        # and the canonical inference runs now (§7). The engine discards
        # anything whose transcript or versions moved; the server re-checks the
        # active turn and transcript hash before honouring the token.
        preview_token = self._anticipation.take(self._preview_key(answer.text))
        self._core_in_flight = True
        self._cancel_task(self._reprompt_task)
        self._submitted_at = perf_counter()
        self._core_completed_at = None
        self.report(
            "voice_answer_submitted",
            since_speech_end_ms=self._ms_since("_speech_ended_at"),
            words=len(answer.text.split()),
            fragments=answer.fragment_count,
            duration_s=answer.duration_seconds,
            confidence=answer.confidence,
            transcription_delay_ms=round(self._last_answer_metrics.get("transcription_delay", 0.0) * 1000)
            if "transcription_delay" in self._last_answer_metrics
            else None,
            end_of_turn_delay_ms=round(self._last_answer_metrics.get("end_of_turn_delay", 0.0) * 1000)
            if "end_of_turn_delay" in self._last_answer_metrics
            else None,
        )
        # The chain this answer travelled: VAD end → final transcript →
        # LiveKit turn end → the window's wait → submit. One event per submit;
        # livekit_turn_metrics.py reads it, the §8 calibration depends on it.
        commit_delay_s = getattr(self, "_commit_delay_s", None)
        commit_effective_s = getattr(self, "_commit_effective_s", None)
        speech_ended_at = getattr(self, "_speech_ended_at", None)
        last_final_at = getattr(self, "_last_final_at", None)
        turn_completed_at = getattr(self, "_turn_completed_at", None)
        self.report(
            "voice_endpointing",
            anchor=commit_anchor(),
            commit_delay_ms=round(commit_delay_s * 1000) if commit_delay_s is not None else None,
            effective_delay_ms=round(commit_effective_s * 1000) if commit_effective_s is not None else None,
            vad_to_final_ms=_delta_ms(speech_ended_at, last_final_at),
            final_to_turn_ms=_delta_ms(last_final_at, turn_completed_at),
            turn_to_submit_ms=_delta_ms(turn_completed_at, self._submitted_at),
            since_speech_end_ms=self._ms_since("_speech_ended_at"),
            fragments=answer.fragment_count,
            nudged=nudged,
        )
        self._publish_turn_state("processing")
        # The reply's TTS must not pay a cold connection after a long answer.
        keepalive = getattr(self, "_tts_keepalive_task", None)
        if getattr(self, "_warm_tts", None) is not None and (keepalive is None or keepalive.done()):
            self._tts_keepalive_task = asyncio.create_task(self._tts_keepalive())

        # The processing backchannel: the Talker sees the whole answer and the
        # phase, and says something about *it* -- or nothing -- while the
        # Thinker works. Runs beside the submit, never awaited by it.
        self._bc().observe(
            answer.text, phase=PHASE_PROCESSING, answer_seconds=answer.duration_seconds, fragments=answer.fragment_count,
        )

        # Keep the local processing filler opt-in; default remains silent while
        # Core works, as the pulled branch specified. Not with backchanneling:
        # those lines are templates.
        filler_spoken = False
        filler_task: asyncio.Task | None = None
        if processing_filler_enabled() and not self._bc().enabled:
            async def filler() -> None:
                nonlocal filler_spoken
                await asyncio.sleep(PROCESSING_FILLER_SECONDS)
                self._filler_count = getattr(self, "_filler_count", 0) + 1
                self.report("voice_processing_filler", after_s=PROCESSING_FILLER_SECONDS)
                line = pick_processing_filler(
                    self._locale,
                    f"{self._session_id}:{self._filler_count}",
                    getattr(self, "_last_filler", None),
                )
                self._last_filler = line
                filler_spoken = True
                self._filler_handle = self.speak(line, allow_interruptions=True, add_to_chat_ctx=False, kind="filler")

            filler_task = asyncio.create_task(filler())
        core_started = perf_counter()
        try:
            response_payload = await self._submit_answer(answer, preview_token, core_started)
            assistant = response_payload.get("assistantResponse")
            content = assistant.get("content") if isinstance(assistant, dict) else None
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("Interview Core returned no voice response")
        except asyncio.CancelledError:
            self._cancel_task(filler_task)
            self._cancel_task(getattr(self, "_tts_keepalive_task", None))
            self._core_in_flight = False
            self._repeat_after_core = False
            raise
        except Exception as exc:
            self._cancel_task(filler_task)
            self._cancel_task(getattr(self, "_tts_keepalive_task", None))
            self._bc().on_reply_ready()
            self._core_in_flight = False
            self._repeat_after_core = False
            self.report(
                "voice_core_failed",
                error_type=type(exc).__name__,
                core_ms=round((perf_counter() - core_started) * 1000),
            )
            self._publish_turn_state("listening")
            logger.exception(
                "Interview Core voice turn failed",
                extra={"session_id": self._session_id, "room_name": self._room_name},
            )
            self.speak(self._core_error_text(), allow_interruptions=True, add_to_chat_ctx=False)
            return
        self._cancel_task(filler_task)
        self._cancel_task(getattr(self, "_tts_keepalive_task", None))
        # The authoritative reply exists: a backchannel still being generated
        # is stale, and one still playing is cut below (_interrupt_filler).
        self._bc().on_reply_ready()
        logger.info(
            "Interview Core voice turn completed",
            extra={
                "session_id": self._session_id,
                "room_name": self._room_name,
                "core_latency_ms": round((perf_counter() - core_started) * 1000),
                "answer_fragments": answer.fragment_count,
                "answer_words": len(answer.text.split()),
                "speculative_evaluation_reused": bool(preview_token),
            },
        )
        is_terminal = bool(
            response_payload.get("sessionStatus") == "CLOSED" or response_payload.get("completed")
        )
        self._core_completed_at = perf_counter()
        self.report(
            "voice_core_completed",
            core_ms=round((perf_counter() - core_started) * 1000),
            action=str(response_payload.get("action") or "")[:40] or None,
            terminal=is_terminal,
        )
        self._core_in_flight = False
        repeat_after_core = self._repeat_after_core
        self._repeat_after_core = False
        leftover = self._answer.take()
        if leftover is not None:
            # Never silently attach a late fragment to the next question. Keep
            # the transcript visible and ask the candidate to restate it for
            # the active question, because Core has already committed the
            # previous turn by this point.
            self.report(
                "voice_late_speech_captured",
                fragments=leftover.fragment_count,
                words=len(leftover.text.split()),
            )
            with contextlib.suppress(Exception):
                await self._room.local_participant.publish_data(
                    json.dumps({"type": "voice.late_transcript", "text": leftover.text}),
                    reliable=True,
                    topic="intervia.turn",
                )
        self._publish_turn_state("listening")
        if repeat_after_core:
            content = f"Mình vừa nhận được yêu cầu nhắc lại. Câu hỏi hiện tại là: {content}"
        if leftover is not None:
            content = (
                "Mình nghe được phần bạn vừa nói thêm. Câu trả lời trước đã được gửi đi rồi. "
                f"Bạn có thể nhắc lại ý đó sau câu hỏi này nhé. {content}"
            )
        # Core asks the candidate to confirm a stop. Chat renders stop/continue
        # buttons in the bubble; the voice room only learns of it from this
        # event, so publish it before speaking and open the dialog right away.
        # The confirmation is not the current question: "continue" must
        # replay the question, not this line.
        is_abort_confirmation = response_payload.get("action") == "CONFIRM_ABORT"
        if is_abort_confirmation:
            with contextlib.suppress(Exception):
                await self._room.local_participant.publish_data(
                    json.dumps({"type": "interview.abort.requested", "sessionId": self._session_id}),
                    reliable=True,
                    topic="intervia.session",
                )
        # A nudge or filler still playing must not delay the interviewer.
        self._interrupt_filler()
        speech = self.speak(
            content,
            allow_interruptions=not is_terminal,
            add_to_chat_ctx=not is_abort_confirmation,
            kind="closing" if is_terminal else "question",
        )
        if is_terminal:
            # Let the candidate hear the deterministic closing before the UI
            # leaves the room, then publish a reliable terminal event.
            await speech.wait_for_playout()
            terminal_payload = json.dumps(
                {
                    "type": "interview.session.closed",
                    "sessionId": self._session_id,
                    "sessionStatus": "CLOSED",
                    "endReason": response_payload.get("endReason") or "COMPLETED",
                }
            )
            await self._room.local_participant.publish_data(
                terminal_payload,
                reliable=True,
                topic="intervia.session",
            )
            if self._on_interview_closed is not None:
                self._on_interview_closed("interview closed by Core")

    def _interrupt_filler(self) -> None:
        """Cut a nudge or filler still playing: the authoritative reply is ready.

        §13 voice-output audit (2026-11-26): in the pilot a nudge was still
        playing when Core answered on 3 of 22 turns, holding the question
        back by up to 0.48 s, because LiveKit plays queued speech in order.
        An interruptible filler is cut; a finished one is left alone.
        """
        handle = getattr(self, "_filler_handle", None)
        self._filler_handle = None
        if handle is None:
            return
        done = getattr(handle, "done", None)
        interrupt = getattr(handle, "interrupt", None)
        if not callable(done) or not callable(interrupt):
            return
        try:
            if done():
                return
            interrupt()
        except Exception:  # noqa: BLE001 - never let the cut fail the reply
            return
        self.report("voice_filler_interrupted")

    def _fixed_lines(self) -> frozenset[str]:
        """Lines identical in every session: their audio is shared on disk."""
        return frozenset(
            {
                *answer_pause_nudges(self._locale),
                silence_reprompt(self._locale),
                hold_acknowledgement(self._locale),
                self._unclear_transcript_text(),
                self._core_error_text(),
                *self._not_heard_lines(),
                turn_taking_tip(self._locale),
                *(processing_fillers(self._locale) if processing_filler_enabled() else ()),
            }
        )

    async def tts_node(self, text, model_settings):
        """Speak notation as words ("CI/CD" → "CI CD") and reuse audio already paid for.

        session.say hands the whole line at once (no LLM streams here), so the
        text is complete before synthesis starts. A fixed line or the question
        being replayed plays from cache; anything else is synthesized and
        cached only if it played to the end, so an interrupted line is never
        stored half-spoken.
        """
        full_text = "".join([chunk async for chunk in text])
        paragraphs = [part.strip() for part in full_text.split("\n\n") if part.strip()]
        if len(paragraphs) > 1:
            # Keep paragraph caching and replay behavior without changing the
            # model's wording. One speech handle preserves interruption behavior.
            for paragraph in paragraphs:
                async for frame in self._synthesize_or_replay(paragraph, model_settings):
                    yield frame
            return
        async for frame in self._synthesize_or_replay(full_text, model_settings):
            yield frame

    async def _synthesize_or_replay(self, full_text: str, model_settings):
        cache = self._speech_cache
        clip = cache.get(full_text) if cache else None
        if clip is not None:
            logger.info(
                "Voice speech replayed from cache",
                extra={
                    "session_id": self._session_id,
                    "room_name": self._room_name,
                    "characters_not_synthesized": len(full_text),
                    "session_characters_not_synthesized": cache.saved_characters,
                    "progressive": isinstance(clip, GrowingClip),
                },
            )
            if isinstance(clip, GrowingClip):
                # Still being synthesized: play what has arrived, wait for the
                # rest (progressive PCM). Underruns are counted on the clip.
                async for piece in clip.stream():
                    yield rtc.AudioFrame(
                        data=piece,
                        sample_rate=clip.sample_rate,
                        num_channels=clip.channels,
                        samples_per_channel=len(piece) // (2 * clip.channels),
                    )
                self.report("voice_backchannel_playout", underruns=clip.underruns, audio_ms=clip.audio_ms,
                            complete=clip.finished and not clip.failed)
                return
            for piece in clip.chunks():
                yield rtc.AudioFrame(
                    data=piece,
                    sample_rate=clip.sample_rate,
                    num_channels=clip.channels,
                    samples_per_channel=len(piece) // (2 * clip.channels),
                )
            return

        async def spoken():
            yield speakable_text(full_text, locale=self._locale)

        pieces: list[bytes] = []
        sample_rate = channels = 0
        async for frame in Agent.default.tts_node(self, spoken(), model_settings):
            pieces.append(bytes(frame.data))
            sample_rate, channels = frame.sample_rate, frame.num_channels
            yield frame
        if cache and pieces and self.cache_writes_enabled:
            cache.put(
                full_text,
                SpeechClip(pcm=b"".join(pieces), sample_rate=sample_rate, channels=channels),
                persist=full_text in self._fixed_lines(),
            )

    def speak(
        self,
        text: str,
        *,
        allow_interruptions: bool = True,
        add_to_chat_ctx: bool = True,
        kind: str | None = None,
    ):
        # question | closing | reprompt | replay | notice (admin view).
        text = localize_particles(text)
        self._active_speech_kind = kind or ("question" if add_to_chat_ctx else "notice")
        handle = self.session.say(
            text,
            allow_interruptions=allow_interruptions,
            add_to_chat_ctx=add_to_chat_ctx,
        )
        self._active_speech_handle = handle
        self._active_speech_text = text
        if add_to_chat_ctx:
            self._last_interview_speech_text = text
        return handle

    async def on_user_turn_completed(self, _turn_ctx, new_message) -> None:
        self._turn_completed_at = perf_counter()
        raw_transcript = new_message.text_content
        logger.info(
            "LiveKit user turn completed",
            extra={
                "session_id": self._session_id,
                "room_name": self._room_name,
                "transcript_characters": len(raw_transcript) if isinstance(raw_transcript, str) else 0,
                "transcript_words": len(raw_transcript.split()) if isinstance(raw_transcript, str) else 0,
            },
        )
        interruption = capture_interruption(
            self._active_speech_handle,
            self._active_speech_text,
        )
        confidence = getattr(new_message, "transcript_confidence", None)
        if self._stt_provider == "qwen":
            # Qwen's chunk API returns text but no calibrated confidence score.
            # Its adapter uses a neutral numeric placeholder for LiveKit internals;
            # do not treat that placeholder as a measured score in quality gating.
            confidence = None
        processed = (
            process_transcript(
                raw_transcript,
                confidence=confidence,
                locale=self._locale,
                context=f"{getattr(self, '_session_context', '')} {self._last_interview_speech_text}".strip(),
                provider=self._stt_provider,
            )
            if isinstance(raw_transcript, str)
            else None
        )
        transcript = processed.text if processed else raw_transcript
        if not isinstance(transcript, str) or not transcript.strip():
            if not self._answer.is_empty:
                # A silent/blank fragment inside an answer: keep waiting.
                self._schedule_commit(self._answer.commit_delay())
                return
            if interruption.occurred:
                resolution = resolve_interruption("", interruption, locale=self._locale)
                if resolution.resume_text:
                    self.report("voice_interruption", action="resume", reason="blank")
                    self.speak(
                        resolution.resume_text,
                        allow_interruptions=True,
                        add_to_chat_ctx=False,
                        kind="replay",
                    )
                return
            self.report("voice_not_heard")
            self.speak(self._not_heard_text(), allow_interruptions=True, add_to_chat_ctx=False)
            return

        if transcript != raw_transcript:
            # Keep the committed conversation context and persisted transcript
            # consistent with the locale correction.
            new_message.content = [transcript]
            logger.info(
                "Corrected locale-specific STT artifact",
                extra={
                    "session_id": self._session_id,
                    "room_name": self._room_name,
                    "raw_transcript": raw_transcript,
                    "corrected_transcript": transcript,
                },
            )

        # The request belongs to the question currently being spoken, not to
        # the answer payload. Core may already be finishing the previous turn;
        # remember the request and repeat the next active question after it
        # returns instead of treating this sentence as an answer or an abort.
        if self._core_in_flight and is_repeat_request(transcript, locale=self._locale):
            self._repeat_after_core = True
            self.report("voice_question_replay_deferred", reason="core_processing")
            return

        quality = processed.quality
        logger.info(
            "Final candidate transcript assessed",
            extra={
                "session_id": self._session_id,
                "room_name": self._room_name,
                "transcript_confidence": quality.confidence,
                "transcript_words": quality.word_count,
                "transcript_quality": quality.reason,
                "transcript_characters": len(transcript),
            },
        )
        if not quality.accepted:
            self._cancel_task(self._commit_task)
            if not self._answer.is_empty:
                self._schedule_commit(self._answer.commit_delay())
                return
            self.report(
                "voice_unclear_transcript",
                reason=str(quality.reason or "")[:40] or None,
                confidence=quality.confidence,
                words=quality.word_count,
            )
            self._publish_turn_state("listening")
            self.speak(self._unclear_transcript_text(), allow_interruptions=True, add_to_chat_ctx=False)
            return

        # A hold owns the conversational floor until the candidate explicitly
        # hands it back or starts a substantive answer.
        if self._held_speech_text:
            if is_repeat_request(transcript, locale=self._locale):
                replay = self._held_speech_text
            elif is_resume_request(transcript, locale=self._locale) or (
                classify_interruption(transcript, locale=self._locale) == "backchannel"
            ):
                replay = self._held_resume_text or self._held_speech_text
            else:
                replay = ""
            self._held_speech_text = ""
            self._held_resume_text = ""
            if replay:
                self.report("voice_question_replayed", reason="after_hold")
                self.speak(replay, allow_interruptions=True, add_to_chat_ctx=False, kind="replay")
                return

        # Greetings just after playout and explicit playback complaints are not
        # interview answers. Replay the complete question as one protected
        # utterance so Core does not advance and the UI can replace its prefix.
        replay_requested = is_repeat_request(transcript, locale=self._locale)
        greeting_only = is_greeting_backchannel(transcript, locale=self._locale) and self._answer.is_empty
        if self._last_interview_speech_text and (
            replay_requested or (greeting_only and not interruption.occurred)
        ):
            # Repeating the question mid-answer keeps what was said so far.
            self._cancel_task(self._commit_task)
            self.report(
                "voice_question_replayed",
                reason="repeat_request" if replay_requested else "greeting",
            )
            self.speak(
                self._last_interview_speech_text,
                allow_interruptions=True,
                add_to_chat_ctx=False,
                kind="replay",
            )
            logger.info(
                "Voice question replayed without advancing interview",
                extra={
                    "session_id": self._session_id,
                    "room_name": self._room_name,
                    "reason": "repeat_request" if replay_requested else "greeting",
                },
            )
            return

        # A standalone acknowledgement after playout is not an answer. Keep
        # listening without replaying the whole question or advancing Core.
        if (
            not interruption.occurred
            and classify_interruption(transcript, locale=self._locale) == "backchannel"
        ):
            if not self._answer.is_empty:
                # "ừm", "vâng" while thinking mid-answer: not a turn of its own.
                self._schedule_commit(self._answer.commit_delay())
                return
            self.speak(
                backchannel_continue_text(self._locale),
                allow_interruptions=True,
                add_to_chat_ctx=False,
            )
            return

        if interruption.occurred:
            resolution = resolve_interruption(transcript, interruption, locale=self._locale)
            if resolution.action == "resume":
                replayed = bool(resolution.resume_text)
                self.report(
                    "voice_interruption", action="resume", reason=str(resolution.reason or "")[:40] or None
                )
                if replayed:
                    self.speak(
                        resolution.resume_text,
                        allow_interruptions=True,
                        add_to_chat_ctx=False,
                        kind="replay",
                    )
                logger.info(
                    "Voice overlap filtered and interviewer playout resumed",
                    extra={
                        "session_id": self._session_id,
                        "room_name": self._room_name,
                        "speech_id": interruption.speech_id,
                        "reason": resolution.reason,
                        "replayed": replayed,
                    },
                )
                return
            if resolution.action == "hold":
                self.report(
                    "voice_interruption", action="hold", reason=str(resolution.reason or "")[:40] or None
                )
                self._held_speech_text = interruption.expected_text
                self._held_resume_text = (
                    remaining_after_interruption(
                        interruption.expected_text,
                        interruption.delivered_text,
                    )
                    or interruption.expected_text
                )
                self.speak(
                    hold_acknowledgement(self._locale),
                    allow_interruptions=True,
                    add_to_chat_ctx=False,
                )
                return

        # Buffer fragments into one answer and submit after adaptive silence.
        metrics = _message_metrics(new_message)
        self._last_answer_metrics = metrics
        self._last_interruption_telemetry = interruption.telemetry()
        self._answer.add(
            transcript,
            message_id=str(new_message.id),
            raw_text=raw_transcript if isinstance(raw_transcript, str) else transcript,
            duration=max(
                0.0,
                float(metrics.get("stopped_speaking_at", 0.0))
                - float(metrics.get("started_speaking_at", 0.0)),
            ),
            confidence=quality.confidence,
        )
        self._schedule_answer_pause_nudge()
        self._schedule_preview(self._answer.text)
        # A fragment just ended: the candidate paused. A safe moment for the
        # Talker to read what they said so far and decide whether to react.
        self._bc().note_transcript(self._answer.text)
        self._bc().observe(
            self._answer.text, phase=PHASE_LISTENING, answer_seconds=self._answer.duration_seconds,
            fragments=len(self._answer.fragments),
        )
        self._cancel_task(self._reprompt_task)
        self._publish_turn_state("answering")
        if not self._core_in_flight:
            self._schedule_commit(self._answer.commit_delay())


# Development mode otherwise keeps zero warm workers. That made the first
# candidate wait roughly 15-20 seconds after joining a room. One idle process
# removes that cold start while remaining configurable for small machines.
server = AgentServer(
    num_idle_processes=_int_env("LIVEKIT_AGENT_IDLE_PROCESSES", 1),
    # The default is finite. Docker restarts the process after this budget is
    # exhausted, while the larger budget absorbs short DNS/LiveKit incidents
    # without throwing away a warm worker.
    max_retry=max(1, _int_env("LIVEKIT_AGENT_MAX_RETRY", 32)),
)


@server.rtc_session(agent_name=os.getenv("LIVEKIT_AGENT_NAME", "intervia-voice"))
async def voice_lab_session(ctx: agents.JobContext) -> None:
    default_voice_id = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    tts_provider = os.getenv("VOICE_LAB_TTS_PROVIDER", "elevenlabs").strip().lower()
    if tts_provider in {"vieneu", "vieneu-tts"}:
        tts_provider = "vieneu"
    elif tts_provider not in {"elevenlabs", "cartesia"}:
        logger.warning("Unknown VOICE_LAB_TTS_PROVIDER=%r; falling back to elevenlabs", tts_provider)
        tts_provider = "elevenlabs"

    runtime_token = (ctx.job.metadata or "").strip()
    if not runtime_token:
        raise RuntimeError("LiveKit dispatch did not include an interview runtime credential")

    api_base_url = os.getenv("VOICE_RUNTIME_API_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
    runtime_client = httpx.AsyncClient(
        base_url=api_base_url,
        headers={"Authorization": f"Bearer {runtime_token}"},
        timeout=httpx.Timeout(90.0),
    )
    if runtime_token.startswith("stt-test:"):
        runtime_client.headers["Authorization"] = f"Bearer {runtime_token.removeprefix('stt-test:')}"
        from src.modules.interviews.realtime.stt.livekit_stt_test import run_stt_test
        try:
            await run_stt_test(ctx, runtime_client)
        finally:
            await runtime_client.aclose()
        return

    try:
        start_response = await runtime_client.post("/ai/voice-lab/livekit/runtime/start")
        start_response.raise_for_status()
        start_payload = start_response.json()
        opening = start_payload.get("openingMessage")
        opening_text = opening.get("content") if isinstance(opening, dict) else None
        session_id = str(start_payload.get("sessionId") or "")
        locale = str(start_payload.get("locale") or "vi-VN")
        candidate_settings = start_payload.get("candidateSettings") or {}
        if not isinstance(candidate_settings, dict):
            candidate_settings = {}
        candidate_voice = str(candidate_settings.get("aiVoice") or "").strip()
        try:
            candidate_speech_rate = min(1.2, max(0.8, float(candidate_settings.get("speechRate", 1.0))))
        except (TypeError, ValueError):
            candidate_speech_rate = 1.0
        if candidate_voice:
            # The Settings voice picker is populated from VieNeu's catalogue.
            tts_provider = "vieneu"
        if not opening_text or not session_id:
            raise RuntimeError("Interview Core returned no opening question")
    except Exception:
        await runtime_client.aclose()
        raise

    async def post_voice_events(batch: list[dict]) -> None:
        response = await runtime_client.post(
            "/ai/voice-lab/livekit/runtime/events", json={"events": batch}, timeout=5.0
        )
        response.raise_for_status()

    # Voice-room state and incidents for the admin view; never blocks the call.
    voice_events = VoiceEventReporter(post_voice_events)

    async def close_runtime_client(_reason: str) -> None:
        with contextlib.suppress(Exception):
            await voice_events.aclose()
        await runtime_client.aclose()

    # Register cleanup before constructing provider clients so a later setup
    # failure cannot leave the authenticated HTTP connection open.
    ctx.add_shutdown_callback(close_runtime_client)

    is_vietnamese = locale.lower().startswith("vi")
    locale_voice_var = "ELEVENLABS_VI_VOICE_ID" if is_vietnamese else "ELEVENLABS_EN_VOICE_ID"
    voice_id = os.getenv(locale_voice_var, "").strip() or default_voice_id
    # "cartesia" speaks through LiveKit Inference only, so development and
    # automated tests do not spend ElevenLabs credits.
    use_elevenlabs = tts_provider == "elevenlabs"
    use_vieneu = tts_provider == "vieneu"
    use_inference = tts_provider == "cartesia"
    if use_elevenlabs and not voice_id:
        raise RuntimeError(f"{locale_voice_var} or ELEVENLABS_VOICE_ID is missing")
    if use_elevenlabs and is_vietnamese and not os.getenv("ELEVENLABS_VI_VOICE_ID", "").strip():
        logger.warning(
            "Vietnamese session is using the generic ElevenLabs voice; configure a Vietnamese-accent voice",
            extra={
                "session_id": session_id,
                "room_name": ctx.room.name,
                "configuration_key": "ELEVENLABS_VI_VOICE_ID",
            },
        )

    stt_settings = stt_provider_settings()
    stt_model = stt_settings.model
    stt_language = _session_stt_language(locale)
    session_context = " ".join(
        part for part in (str(start_payload.get("jobTitle") or "").strip(), opening_text.strip()) if part
    )
    stt = create_stt(stt_settings, stt_language, session_context,
                     telemetry=lambda fields: voice_events.emit("voice_stt_diagnostic", **fields))

    interruption_mode = os.getenv("VOICE_LAB_INTERRUPTION_MODE", "adaptive").strip().lower()
    if interruption_mode not in {"adaptive", "vad"}:
        logger.warning("Unknown interruption mode %r; falling back to vad", interruption_mode)
        interruption_mode = "vad"
    tuning = voice_tuning()
    tts_config = tts_provider_config(locale) if use_elevenlabs else None

    logger.info(
        "Voice session language locked",
        extra={
            "session_id": session_id,
            "room_name": ctx.room.name,
            "locale": locale,
            "stt_language": stt_language,
            "stt_provider": stt_settings.provider,
            "stt_model": stt_model,
            "tts_provider": tts_provider,
            "tts_language": tts_config.language if tts_config else "auto",
        },
    )

    tts_language = {"language": tts_config.language} if tts_config and tts_config.language else {}
    fallback_model = os.getenv("VOICE_LAB_FALLBACK_TTS_MODEL", "cartesia/sonic-3").strip()
    fallback_voice = os.getenv("VOICE_LAB_FALLBACK_TTS_VOICE", "").strip()
    # Pinning "vi" reads English terms with Vietnamese phonology; auto
    # kept them intelligible (15/15 vs 13/15 English-only STT check).
    fallback_language = os.getenv("VOICE_LAB_FALLBACK_TTS_LANGUAGE", "auto").strip().lower()
    inference_tts = (
        inference.TTS(
            model=fallback_model,
            **({} if fallback_language == "auto" else {"language": "vi" if is_vietnamese else "en"}),
            **({"voice": fallback_voice} if fallback_voice else {}),
        )
        if fallback_model and (use_elevenlabs or use_inference or use_vieneu)
        else None
    )
    if use_inference and inference_tts is None:
        raise RuntimeError("VOICE_LAB_TTS_PROVIDER=cartesia needs VOICE_LAB_FALLBACK_TTS_MODEL")
    if use_vieneu:
        vieneu_api_key = (
            os.getenv("VIENEU_TTS_API_KEY", "").strip() or os.getenv("VIENEU_API_KEY", "").strip()
        )
        try:
            vieneu_timeout = float(os.getenv("VIENEU_TTS_TIMEOUT_SECONDS", "180"))
        except ValueError:
            vieneu_timeout = 180.0
        primary_tts = VieNeuTTS(
            base_url=os.getenv("VIENEU_TTS_BASE_URL", "http://host.docker.internal:8000"),
            api_key=vieneu_api_key,
            voice=candidate_voice or os.getenv("VIENEU_TTS_VOICE", ""),
            model=os.getenv("VIENEU_TTS_MODEL", ""),
            speed=candidate_speech_rate,
            timeout_seconds=vieneu_timeout,
            sample_rate=_int_env("VIENEU_TTS_SAMPLE_RATE", 16000),
        )
    elif use_elevenlabs:
        assert tts_config is not None
        primary_tts = elevenlabs.TTS(
            voice_id=voice_id,
            model=tts_config.model,
            **tts_language,
            auto_mode=tts_config.auto_mode,
            apply_text_normalization=tts_config.apply_text_normalization,
            voice_settings=elevenlabs.VoiceSettings(
                stability=tts_config.stability,
                similarity_boost=tts_config.similarity_boost,
                style=tts_config.style,
                speed=tts_config.speed,
                use_speaker_boost=tts_config.use_speaker_boost,
            ),
        )
    else:
        assert inference_tts is not None
        primary_tts = inference_tts
    # An exhausted ElevenLabs quota, or a self-hosted VieNeu server that is not
    # running, answers every request with no audio, and a session with a
    # single TTS then closes as unrecoverable: the candidate hears nothing.
    # LiveKit Inference reuses the LiveKit credentials STT already needs, so
    # the fallback adds no key. An empty model disables it.
    tts_providers: list = [primary_tts]
    if (use_elevenlabs or use_vieneu) and inference_tts is not None:
        tts_providers.append(inference_tts)
    voice_signature = (
        f"elevenlabs|{voice_id}|{json.dumps(tts_config.telemetry(), sort_keys=True)}|{locale}"
        if use_elevenlabs
        else (
            f"{tts_provider}|{primary_tts.model}|{candidate_voice or os.getenv('VIENEU_TTS_VOICE', '')}|"
            f"{candidate_speech_rate}|{locale}"
            if use_vieneu
            else f"inference|{fallback_model}|{fallback_voice}|{fallback_language}|{locale}"
        )
    )
    speech_cache = SpeechAudioCache(
        voice_signature=voice_signature,
        directory=Path(os.getenv("VOICE_LAB_TTS_CACHE_DIR", "/tmp/intervia-tts-cache")) / "fixed"
        if os.getenv("VOICE_LAB_TTS_CACHE_DIR", "/tmp/intervia-tts-cache").strip()
        else None,
    )
    tts = (
        agents.tts.FallbackAdapter(
            tts_providers,
            # Fail over on the first rejected request; each retry is a pause
            # the candidate hears as dead air.
            max_retry_per_tts=_int_env("VOICE_LAB_TTS_RETRIES_BEFORE_FALLBACK", 1),
        )
        if len(tts_providers) > 1
        else primary_tts
    )

    degraded_components: set[str] = set()

    def publish_health(payload: dict) -> None:
        with contextlib.suppress(Exception):
            asyncio.create_task(
                ctx.room.local_participant.publish_data(
                    json.dumps(payload),
                    reliable=True,
                    topic="intervia.health",
                )
            )

    def mark_provider_recovered(component: str) -> None:
        if component not in degraded_components:
            return
        degraded_components.discard(component)
        publish_health(provider_recovered_payload(session_id=session_id, component=component))
        voice_events.emit("voice_provider_recovered", component=component)
        logger.info(
            "Voice provider recovered",
            extra={"session_id": session_id, "room_name": ctx.room.name, "component": component},
        )

    voice_events.emit(
        "voice_session_started",
        stt_model=stt_model,
        tts_provider=tts_provider,
        interruption_mode=interruption_mode,
    )

    @stt.on("metrics_collected")
    def on_stt_metrics(metric) -> None:
        mark_provider_recovered("stt")
        logger.info(
            "Voice STT metrics",
            extra={
                "session_id": session_id,
                "room_name": ctx.room.name,
                "provider": getattr(getattr(metric, "metadata", None), "provider", None),
                "model": getattr(getattr(metric, "metadata", None), "model_name", None),
                "duration_ms": round(float(metric.duration) * 1000),
                "audio_duration_ms": round(float(metric.audio_duration) * 1000),
                "streamed": bool(metric.streamed),
            },
        )

    if tts is not primary_tts:

        @tts.on("tts_availability_changed")
        def on_tts_availability(event) -> None:
            # While the primary voice is down, speech may come from the
            # fallback voice: never file that audio under the primary voice.
            if event.tts is primary_tts:
                agent.cache_writes_enabled = bool(event.available)
            voice_events.emit(
                "voice_tts_fallback",
                provider=str(getattr(event.tts, "provider", type(event.tts).__name__))[:40],
                available=bool(event.available),
                primary=event.tts is primary_tts,
            )
            logger.warning(
                "Voice TTS provider availability changed",
                extra={
                    "session_id": session_id,
                    "room_name": ctx.room.name,
                    "provider": getattr(event.tts, "provider", type(event.tts).__name__),
                    "model": getattr(event.tts, "model", None),
                    "available": bool(event.available),
                },
            )

    @tts.on("metrics_collected")
    def on_tts_metrics(metric) -> None:
        mark_provider_recovered("tts")
        # Which utterance this synthesis was for (question / early_ack / filler /
        # replay ...), so VieNeu first-byte can be read per kind (§13). The
        # agent is created further down this function; a metric that fires
        # before that (there is none today) simply has no kind.
        try:
            speech_kind = agent._active_speech_kind
        except (NameError, AttributeError):
            speech_kind = None
        with contextlib.suppress(Exception):
            voice_events.emit(
                "voice_tts_metrics",
                kind=speech_kind,
                provider=str(getattr(getattr(metric, "metadata", None), "provider", "") or "")[:40] or None,
                ttfb_ms=round(float(metric.ttfb) * 1000),
                audio_ms=round(float(metric.audio_duration) * 1000),
                characters=int(metric.characters_count),
                cancelled=bool(metric.cancelled),
            )
        logger.info(
            "Voice TTS metrics",
            extra={
                "session_id": session_id,
                "room_name": ctx.room.name,
                "provider": getattr(getattr(metric, "metadata", None), "provider", None),
                "model": getattr(getattr(metric, "metadata", None), "model_name", None),
                "ttfb_ms": round(float(metric.ttfb) * 1000),
                "duration_ms": round(float(metric.duration) * 1000),
                "audio_duration_ms": round(float(metric.audio_duration) * 1000),
                "characters": int(metric.characters_count),
                "cancelled": bool(metric.cancelled),
            },
        )

    session = AgentSession(
        stt=stt, tts=tts,
        **recognition_options(stt_settings, tuning, interruption=InterruptionOptions(
                enabled=True,
                mode=interruption_mode,
                min_duration=tuning.interruption_min_duration,
                min_words=_int_env("VOICE_LAB_INTERRUPTION_MIN_WORDS", 2),
                resume_false_interruption=True,
                false_interruption_timeout=tuning.false_interruption_timeout,
                backchannel_boundary=(
                    _float_env("VOICE_LAB_BACKCHANNEL_START_BOUNDARY", 0.5),
                    _float_env("VOICE_LAB_BACKCHANNEL_END_BOUNDARY", 2.2),
                ),
            )),
    )

    @session.on("error")
    def on_provider_error(event) -> None:
        payload = provider_error_payload(event, session_id=session_id)
        component = str(payload["component"])
        degraded_components.add(component)
        publish_health(payload)
        voice_events.emit(
            "voice_provider_degraded",
            component=component,
            provider=str(payload.get("provider") or "")[:40] or None,
            recoverable=bool(payload.get("recoverable")),
        )
        provider_exception = getattr(getattr(event, "error", None), "error", None)
        logger.warning(
            "Voice provider degraded",
            exc_info=(
                type(provider_exception),
                provider_exception,
                provider_exception.__traceback__,
            )
            if isinstance(provider_exception, BaseException)
            else None,
            extra={
                "session_id": session_id,
                "room_name": ctx.room.name,
                "component": component,
                "provider": payload["provider"],
                "recoverable": payload["recoverable"],
            },
        )

    @session.on("overlapping_speech")
    def on_overlapping_speech(event) -> None:
        logger.info(
            "Adaptive interruption decision",
            extra={
                "session_id": session_id,
                "room_name": ctx.room.name,
                "is_interruption": event.is_interruption,
                "probability": round(float(event.probability), 4),
                "detection_delay_ms": round(float(event.detection_delay) * 1000),
                "prediction_duration_ms": round(float(event.prediction_duration) * 1000),
            },
        )

    @session.on("agent_false_interruption")
    def on_false_interruption(event) -> None:
        logger.info(
            "False interruption recovered",
            extra={
                "session_id": session_id,
                "room_name": ctx.room.name,
                "resumed": event.resumed,
            },
        )

    # LiveKit Inference counts every open STT stream against the plan's
    # concurrency cap (Build: 5). A session left open after the interview, or
    # after the candidate closed the tab (close_on_disconnect only covers a
    # client-initiated leave), held its stream until the room timed out, and
    # back-to-back sessions then failed with 429. Release it explicitly.
    closing = False

    def release_session(reason: str, *, delay: float = 1.0) -> None:
        nonlocal closing
        if closing:
            return
        closing = True

        voice_events.emit("voice_session_released", reason=reason[:60])

        async def _release() -> None:
            # Let the terminal data packet reach the browser first.
            await asyncio.sleep(delay)
            logger.info(
                "Voice session released",
                extra={"session_id": session_id, "room_name": ctx.room.name, "reason": reason},
            )
            with contextlib.suppress(Exception):
                await session.aclose()
            ctx.shutdown(reason=reason)

        asyncio.create_task(_release())

    agent = VoiceLabAgent(
        runtime_client=runtime_client,
        session_id=session_id,
        room_name=ctx.room.name,
        room=ctx.room,
        locale=locale,
        session_context=str(start_payload.get("jobTitle") or ""),
        stt_provider=stt_settings.provider,
        enable_speculative_preview=(
            stt_settings.provider == "qwen" and _bool_env("VOICE_LLM_SPECULATIVE_PREVIEW", True)
        ),
        speech_cache=speech_cache,
        on_interview_closed=release_session,
        events=voice_events,
        warm_tts=primary_tts.warm if use_vieneu else None,
        # Context-aware backchanneling: the API also checks the setting and the
        # Talker model; the agent's flag only decides whether it asks.
        enable_backchannel=_bool_env("INTERVIEW_BACKCHANNEL_ENABLED", False),
        backchannel_calls_per_turn=_int_env("INTERVIEW_BACKCHANNEL_CALLS_PER_TURN", 3) or 3,
        backchannel_min_gap_seconds=_float_env("INTERVIEW_BACKCHANNEL_MIN_GAP_SECONDS", 4.0),
        prepare_tts=primary_tts if use_vieneu else None,
    )

    abandon_grace = _float_env("VOICE_LAB_ABANDON_GRACE_SECONDS", 20.0)
    abandon_task: asyncio.Task | None = None

    def _candidate_present() -> bool:
        local_identity = ctx.room.local_participant.identity
        return any(identity != local_identity for identity in ctx.room.remote_participants)

    async def _release_if_abandoned() -> None:
        try:
            await asyncio.sleep(abandon_grace)
        except asyncio.CancelledError:
            return
        if not _candidate_present():
            # Tell Core first (the client closes with the session): the reaper
            # closes the interview as CANDIDATE_LEFT unless the candidate rejoins.
            with contextlib.suppress(Exception):
                await runtime_client.post("/ai/voice-lab/livekit/runtime/left", timeout=5.0)
            release_session("candidate left the room", delay=0.0)

    @ctx.room.on("participant_disconnected")
    def on_candidate_left(_participant) -> None:
        # Any reason (tab closed, network lost): wait briefly for a reconnect.
        nonlocal abandon_task
        if _candidate_present():
            return
        voice_events.emit("voice_candidate_left", grace_s=abandon_grace)
        agent.on_user_away()
        if abandon_task is not None and not abandon_task.done():
            abandon_task.cancel()
        abandon_task = asyncio.create_task(_release_if_abandoned())

    @ctx.room.on("participant_connected")
    def on_candidate_back(_participant) -> None:
        voice_events.emit("voice_candidate_returned")
        if abandon_task is not None and not abandon_task.done():
            abandon_task.cancel()

    @session.on("user_state_changed")
    def on_user_state(event) -> None:
        if event.new_state == "speaking":
            agent.on_user_speaking()
        elif event.new_state == "away":
            agent.on_user_away()
        elif event.old_state == "speaking":
            agent.on_user_stopped()

    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event) -> None:
        if isinstance(event.transcript, str):
            logger.info(
                "LiveKit STT transcript received",
                extra={
                    "session_id": session_id,
                    "room_name": ctx.room.name,
                    "provider": stt_settings.provider,
                    "is_final": bool(event.is_final),
                    "transcript_characters": len(event.transcript),
                },
            )
            if event.is_final:
                agent.on_final_transcript()
            else:
                agent.on_interim_transcript(event.transcript)

    @session.on("agent_state_changed")
    def on_agent_state(event) -> None:
        if event.new_state == "speaking":
            agent.on_agent_speaking()
        elif event.old_state == "speaking" and event.new_state == "listening":
            agent.on_agent_listening()

    @ctx.room.on("data_received")
    def on_data_received(packet) -> None:
        if getattr(packet, "topic", None) != "intervia.answer":
            return
        try:
            message = json.loads(bytes(packet.data).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(message, dict):
            agent.handle_client_message(message)

    await session.start(
        room=ctx.room,
        agent=agent,
        room_options=recognition_room_options(tuning),
    )
    # Do not let startup noise or echo turn the opening question into a
    # candidate answer before AEC has converged. The spoken opening combines
    # a brief greeting with the first question in one utterance.
    # "Repeat" replays the most recent question.
    for paragraph in opening_paragraphs(opening_text):
        await agent.speak(paragraph, allow_interruptions=False)


if __name__ == "__main__":
    agents.cli.run_app(server)
