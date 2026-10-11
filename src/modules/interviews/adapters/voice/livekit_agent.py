"""LiveKit voice adapter for the authenticated Interview Core runtime.

The API lifespan starts this worker automatically when ``LIVEKIT_AGENT_AUTOSTART``
is enabled. It keeps streaming speech recognition and ElevenLabs TTS, while
Interview Core supplies every interview question and response.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from time import perf_counter

import httpx
from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, TurnHandlingOptions, inference, room_io
from livekit.agents.voice.turn import EndpointingOptions, InterruptionOptions
from livekit.plugins import elevenlabs

try:
    from livekit.plugins import noise_cancellation
except ImportError:  # Allows a rolling deploy before the optional wheel is present.
    noise_cancellation = None  # type: ignore[assignment]

try:
    from .answer_window import (
        PROCESSING_FILLER_SECONDS,
        AnswerWindow,
        is_done_command,
        processing_filler,
        reprompt_after_seconds,
        silence_reprompt,
    )
    from .health import provider_error_payload, provider_recovered_payload
    from .interruption import (
        capture_interruption,
        classify_interruption,
        hold_acknowledgement,
        is_greeting_backchannel,
        is_repeat_request,
        is_resume_request,
        remaining_after_interruption,
        resolve_interruption,
    )
    from .language import assemblyai_session_options, normalize_transcript_for_locale
    from .provider_config import tts_provider_config
    from .transcript_quality import assess_transcript
    from .tuning import voice_tuning
except ImportError:  # Executed directly by the API's LiveKit subprocess.
    from answer_window import (  # type: ignore[no-redef]
        PROCESSING_FILLER_SECONDS,
        AnswerWindow,
        is_done_command,
        processing_filler,
        reprompt_after_seconds,
        silence_reprompt,
    )
    from health import provider_error_payload, provider_recovered_payload  # type: ignore[no-redef]
    from interruption import (  # type: ignore[no-redef]
        capture_interruption,
        classify_interruption,
        hold_acknowledgement,
        is_greeting_backchannel,
        is_repeat_request,
        is_resume_request,
        remaining_after_interruption,
        resolve_interruption,
    )
    from language import assemblyai_session_options, normalize_transcript_for_locale  # type: ignore[no-redef]
    from provider_config import tts_provider_config  # type: ignore[no-redef]
    from transcript_quality import assess_transcript  # type: ignore[no-redef]
    from tuning import voice_tuning  # type: ignore[no-redef]

# Prefer variables supplied by Docker/the backend environment. The project-root
# .env file only fills values that are not already present.
load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)

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


def _noise_filter(mode: str):
    if mode == "off":
        return None
    if noise_cancellation is None:
        logger.warning(
            "Server noise cancellation requested but plugin is unavailable; continuing with WebRTC filtering"
        )
        return None
    return noise_cancellation.BVC()


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


def _session_stt_language(locale: str) -> str:
    """Keep recognition in the interview language for the whole session."""
    configured = os.getenv("LIVEKIT_STT_LANGUAGE", "session").strip().lower()
    if configured not in {"", "auto", "multi", "session"}:
        return configured
    return "vi" if locale.lower().startswith("vi") else "en"


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
    return {
        name: round(float(raw[name]), 4)
        for name in names
        if isinstance(raw.get(name), (int, float))
    }


class VoiceLabAgent(Agent):
    def __init__(
        self,
        *,
        runtime_client: httpx.AsyncClient,
        session_id: str,
        room_name: str,
        room: object,
        locale: str,
    ) -> None:
        super().__init__(instructions=AGENT_INSTRUCTIONS)
        self._runtime_client = runtime_client
        self._session_id = session_id
        self._room_name = room_name
        self._room = room
        self._locale = locale
        self._active_speech_handle: object | None = None
        self._active_speech_text = ""
        self._last_interview_speech_text = ""
        self._held_speech_text = ""
        self._held_resume_text = ""
        # Answer window: fragments of one spoken answer, submitted together.
        self._answer = AnswerWindow()
        self._commit_task: asyncio.Task | None = None
        self._core_in_flight = False
        self._reprompt_task: asyncio.Task | None = None
        self._reprompted_text = ""
        self._last_interruption_telemetry: dict = {}

    # ── Answer window ────────────────────────────────────────────────────────

    def _publish_turn_state(self, state: str) -> None:
        """Tell the room UI where the answer is: answering | processing | listening."""
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
        self._commit_task = asyncio.create_task(self._commit_after(delay))

    async def _commit_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self._commit_answer()

    def on_user_speaking(self) -> None:
        """The candidate is (still) talking: the answer is not finished yet."""
        self._cancel_task(self._commit_task)
        self._cancel_task(self._reprompt_task)

    def on_agent_listening(self) -> None:
        """A question finished playing: start waiting for the answer."""
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
            self.speak(silence_reprompt(self._locale), allow_interruptions=True, add_to_chat_ctx=False)

    def handle_client_message(self, message: dict) -> None:
        """Data messages from the room UI (topic ``intervia.answer``)."""
        kind = message.get("type")
        if kind == "answer.commit":
            self._cancel_task(self._commit_task)
            if self._answer.is_empty:
                self.speak(self._nothing_to_submit_text(), allow_interruptions=True, add_to_chat_ctx=False)
                return
            self._commit_task = asyncio.create_task(self._commit_answer())
        elif kind == "question.repeat" and self._last_interview_speech_text:
            self.speak(self._last_interview_speech_text, allow_interruptions=True, add_to_chat_ctx=False)

    def _nothing_to_submit_text(self) -> str:
        if self._locale.lower().startswith("vi"):
            return "Mình chưa nghe câu trả lời nào cho câu này. Bạn cứ trình bày nhé."
        return "I have not heard an answer to this question yet. Go ahead."

    def _unclear_transcript_text(self) -> str:
        if self._locale.lower().startswith("vi"):
            return (
                "Mình nghe chưa đủ rõ nên chưa ghi nhận câu này. "
                "Bạn vui lòng nói lại chậm và gần micro hơn nhé."
            )
        return (
            "I could not hear that clearly enough to record it. "
            "Please speak again, a little slower and closer to the microphone."
        )

    async def _commit_answer(self) -> None:
        """Submit the buffered answer to Interview Core and speak its reply."""
        if self._core_in_flight:
            return  # speech that arrives meanwhile waits for the next question
        answer = self._answer.take()
        if answer is None:
            return
        self._core_in_flight = True
        self._cancel_task(self._reprompt_task)
        self._publish_turn_state("processing")

        async def filler() -> None:
            await asyncio.sleep(PROCESSING_FILLER_SECONDS)
            self.speak(processing_filler(self._locale), allow_interruptions=True, add_to_chat_ctx=False)

        filler_task = asyncio.create_task(filler())
        core_started = perf_counter()
        try:
            response = await self._runtime_client.post(
                "/ai/voice-lab/livekit/runtime/message",
                json={
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
                },
            )
            response.raise_for_status()
            response_payload = response.json()
            assistant = response_payload.get("assistantResponse")
            content = assistant.get("content") if isinstance(assistant, dict) else None
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("Interview Core returned no voice response")
        except Exception:
            self._cancel_task(filler_task)
            self._core_in_flight = False
            self._publish_turn_state("listening")
            logger.exception(
                "Interview Core voice turn failed",
                extra={"session_id": self._session_id, "room_name": self._room_name},
            )
            self.speak(
                "Hiện tại mình chưa xử lý được câu trả lời. Bạn vui lòng thử lại nhé."
                if self._locale.lower().startswith("vi")
                else "I could not process that answer. Please try again.",
                allow_interruptions=True,
            )
            return
        self._cancel_task(filler_task)
        logger.info(
            "Interview Core voice turn completed",
            extra={
                "session_id": self._session_id,
                "room_name": self._room_name,
                "core_latency_ms": round((perf_counter() - core_started) * 1000),
                "answer_fragments": answer.fragment_count,
                "answer_words": len(answer.text.split()),
            },
        )
        is_terminal = bool(
            response_payload.get("sessionStatus") == "CLOSED" or response_payload.get("completed")
        )
        self._core_in_flight = False
        leftover = self._answer.take()
        if leftover is not None:
            # Speech that arrived while Core was working is the tail of the
            # answer just submitted, not an answer to the question about to be
            # asked; submitting it would answer the next question with it.
            logger.info(
                "Speech during Core processing dropped",
                extra={"session_id": self._session_id, "fragments": leftover.fragment_count},
            )
        self._publish_turn_state("listening")
        speech = self.speak(content, allow_interruptions=not is_terminal)
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

    def speak(
        self,
        text: str,
        *,
        allow_interruptions: bool = True,
        add_to_chat_ctx: bool = True,
    ):
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
        raw_transcript = new_message.text_content
        transcript = (
            normalize_transcript_for_locale(raw_transcript, locale=self._locale)
            if isinstance(raw_transcript, str)
            else raw_transcript
        )
        interruption = capture_interruption(
            self._active_speech_handle,
            self._active_speech_text,
        )
        confidence = getattr(new_message, "transcript_confidence", None)
        if not isinstance(transcript, str) or not transcript.strip():
            if not self._answer.is_empty:
                # A silent/blank fragment inside an answer: keep waiting.
                self._schedule_commit(self._answer.commit_delay())
                return
            if interruption.occurred:
                resolution = resolve_interruption("", interruption, locale=self._locale)
                if resolution.resume_text:
                    self.speak(
                        resolution.resume_text,
                        allow_interruptions=True,
                        add_to_chat_ctx=False,
                    )
                return
            self.speak(
                "M\u00ecnh ch\u01b0a nghe r\u00f5 c\u00e2u tr\u1ea3 l\u1eddi. "
                "B\u1ea1n vui l\u00f2ng n\u00f3i l\u1ea1i nh\u00e9.",
                allow_interruptions=True,
            )
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

        quality = assess_transcript(transcript, confidence=confidence, locale=self._locale)
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
                self.speak(replay, allow_interruptions=True, add_to_chat_ctx=False)
                return

        # Greetings just after playout and explicit playback complaints are not
        # interview answers. Replay the complete question as one protected
        # utterance so Core does not advance and the UI can replace its prefix.
        # "Xong" / "em trả lời xong" hands the floor back: submit now.
        if is_done_command(transcript, locale=self._locale):
            self._cancel_task(self._commit_task)
            if self._answer.is_empty:
                self.speak(self._nothing_to_submit_text(), allow_interruptions=True, add_to_chat_ctx=False)
            else:
                self._commit_task = asyncio.create_task(self._commit_answer())
            return

        replay_requested = is_repeat_request(transcript, locale=self._locale)
        greeting_only = is_greeting_backchannel(transcript, locale=self._locale) and self._answer.is_empty
        if self._last_interview_speech_text and (
            replay_requested or (greeting_only and not interruption.occurred)
        ):
            # Repeating the question mid-answer keeps what was said so far.
            self._cancel_task(self._commit_task)
            self.speak(
                self._last_interview_speech_text,
                allow_interruptions=True,
                add_to_chat_ctx=False,
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
                "Vâng, bạn cứ tiếp tục nhé."
                if self._locale.lower().startswith("vi")
                else "Sure, go ahead.",
                allow_interruptions=True,
                add_to_chat_ctx=False,
            )
            return

        if interruption.occurred:
            resolution = resolve_interruption(transcript, interruption, locale=self._locale)
            if resolution.action == "resume":
                replayed = bool(resolution.resume_text)
                if replayed:
                    self.speak(
                        resolution.resume_text,
                        allow_interruptions=True,
                        add_to_chat_ctx=False,
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

        # Buffer the fragment; the whole answer is submitted after a longer
        # silence, on "xong", or when the candidate presses the Xong button.
        metrics = _message_metrics(new_message)
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

    runtime_token = (ctx.job.metadata or "").strip()
    if not runtime_token:
        raise RuntimeError("LiveKit dispatch did not include an interview runtime credential")

    api_base_url = os.getenv("VOICE_RUNTIME_API_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
    runtime_client = httpx.AsyncClient(
        base_url=api_base_url,
        headers={"Authorization": f"Bearer {runtime_token}"},
        timeout=httpx.Timeout(90.0),
    )
    try:
        start_response = await runtime_client.post("/ai/voice-lab/livekit/runtime/start")
        start_response.raise_for_status()
        start_payload = start_response.json()
        opening = start_payload.get("openingMessage")
        opening_text = opening.get("content") if isinstance(opening, dict) else None
        session_id = str(start_payload.get("sessionId") or "")
        locale = str(start_payload.get("locale") or "vi-VN")
        if not opening_text or not session_id:
            raise RuntimeError("Interview Core returned no opening question")
    except Exception:
        await runtime_client.aclose()
        raise

    async def close_runtime_client(_reason: str) -> None:
        await runtime_client.aclose()

    # Register cleanup before constructing provider clients so a later setup
    # failure cannot leave the authenticated HTTP connection open.
    ctx.add_shutdown_callback(close_runtime_client)

    is_vietnamese = locale.lower().startswith("vi")
    locale_voice_var = "ELEVENLABS_VI_VOICE_ID" if is_vietnamese else "ELEVENLABS_EN_VOICE_ID"
    voice_id = os.getenv(locale_voice_var, "").strip() or default_voice_id
    if not voice_id:
        raise RuntimeError(f"{locale_voice_var} or ELEVENLABS_VOICE_ID is missing")
    if is_vietnamese and not os.getenv("ELEVENLABS_VI_VOICE_ID", "").strip():
        logger.warning(
            "Vietnamese session is using the generic ElevenLabs voice; configure a Vietnamese-accent voice",
            extra={
                "session_id": session_id,
                "room_name": ctx.room.name,
                "configuration_key": "ELEVENLABS_VI_VOICE_ID",
            },
        )

    stt_model = os.getenv("LIVEKIT_STT_MODEL", "assemblyai/universal-3-6-pro").strip()
    is_assemblyai = stt_model.startswith("assemblyai/")
    stt_language = _session_stt_language(locale)
    if is_assemblyai:
        assemblyai_options: dict[str, object] = {
            **assemblyai_session_options(stt_language, agent_context=opening_text),
            # Keep natural pauses inside one answer instead of finalizing
            # every short clause as a separate turn.
            "mode": "balanced",
            "min_turn_silence": _int_env("VOICE_LAB_STT_MIN_TURN_SILENCE_MS", 600),
            "max_turn_silence": _int_env("VOICE_LAB_STT_MAX_TURN_SILENCE_MS", 2000),
            "vad_threshold": _float_env("VOICE_LAB_STT_VAD_THRESHOLD", 0.3),
        }
        stt = inference.STT(
            model=stt_model,
            language=stt_language,
            extra_kwargs=assemblyai_options,
        )
    else:
        stt = inference.STT(
            model=stt_model,
            language=stt_language,
        )

    interruption_mode = os.getenv("VOICE_LAB_INTERRUPTION_MODE", "adaptive").strip().lower()
    if interruption_mode not in {"adaptive", "vad"}:
        logger.warning("Unknown interruption mode %r; falling back to vad", interruption_mode)
        interruption_mode = "vad"
    tuning = voice_tuning()
    tts_config = tts_provider_config(locale)

    logger.info(
        "Voice session language locked",
        extra={
            "session_id": session_id,
            "room_name": ctx.room.name,
            "locale": locale,
            "stt_language": stt_language,
            "stt_model": stt_model,
            "tts_language": tts_config.language or "auto",
        },
    )

    tts_language = {"language": tts_config.language} if tts_config.language else {}
    tts = elevenlabs.TTS(
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
        logger.info(
            "Voice provider recovered",
            extra={"session_id": session_id, "room_name": ctx.room.name, "component": component},
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

    @tts.on("metrics_collected")
    def on_tts_metrics(metric) -> None:
        mark_provider_recovered("tts")
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
        # Vietnamese mode is more reliable for short utterances. Set the env
        # value to "multi" only when the candidate frequently code-switches.
        stt=stt,
        # STT decides end-of-turn. Silero remains deliberately more sensitive
        # so quiet Vietnamese syllables and real barge-ins are not discarded.
        vad=inference.VAD(
            model="silero",
            min_speech_duration=tuning.vad_min_speech_duration,
            min_silence_duration=tuning.vad_min_silence_duration,
            activation_threshold=tuning.vad_activation_threshold,
            deactivation_threshold=tuning.vad_deactivation_threshold,
        ),
        # Turn callback forwards transcripts to Interview Core; no free-form LLM is attached.
        llm=None,
        tts=tts,
        # Browser echo cancellation is already enabled. A shorter warmup keeps
        # the opening interruptible without removing AEC protection entirely.
        aec_warmup_duration=tuning.aec_warmup_duration,
        # Leave a human-sized breath between the immediate acknowledgement and
        # the full Interview Core response queued behind it.
        min_consecutive_speech_delay=tuning.min_consecutive_speech_delay,
        # End a user turn after a short, stable silence. This makes the lab
        # predictable for measurement and prevents waiting for manual input.
        turn_handling=TurnHandlingOptions(
            turn_detection="stt" if is_assemblyai else "vad",
            endpointing=EndpointingOptions(
                mode="fixed",
                min_delay=0.0 if is_assemblyai else 0.8,
                max_delay=tuning.endpoint_max_delay,
            ),
            interruption=InterruptionOptions(
                enabled=True,
                mode=interruption_mode,
                min_duration=tuning.interruption_min_duration,
                min_words=_int_env("VOICE_LAB_INTERRUPTION_MIN_WORDS", 1),
                resume_false_interruption=True,
                false_interruption_timeout=tuning.false_interruption_timeout,
                backchannel_boundary=(
                    _float_env("VOICE_LAB_BACKCHANNEL_START_BOUNDARY", 0.5),
                    _float_env("VOICE_LAB_BACKCHANNEL_END_BOUNDARY", 2.2),
                ),
            ),
        ),
    )

    @session.on("error")
    def on_provider_error(event) -> None:
        payload = provider_error_payload(event, session_id=session_id)
        component = str(payload["component"])
        degraded_components.add(component)
        publish_health(payload)
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

    agent = VoiceLabAgent(
        runtime_client=runtime_client,
        session_id=session_id,
        room_name=ctx.room.name,
        room=ctx.room,
        locale=locale,
    )
    @session.on("user_state_changed")
    def on_user_state(event) -> None:
        if event.new_state == "speaking":
            agent.on_user_speaking()

    @session.on("agent_state_changed")
    def on_agent_state(event) -> None:
        if event.old_state == "speaking" and event.new_state == "listening":
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
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=_noise_filter(tuning.noise_cancellation),
                auto_gain_control=True,
            )
        ),
    )
    # Do not let startup noise or echo turn the opening question into a
    # candidate answer before AEC has converged.
    await agent.speak(opening_text, allow_interruptions=False)


if __name__ == "__main__":
    agents.cli.run_app(server)
