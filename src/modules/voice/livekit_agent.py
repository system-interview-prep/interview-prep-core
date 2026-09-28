"""LiveKit voice adapter for the authenticated Interview Core runtime.

The API lifespan starts this worker automatically when ``LIVEKIT_AGENT_AUTOSTART``
is enabled. It keeps streaming speech recognition and ElevenLabs TTS, while
Interview Core supplies every interview question and response.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, TurnHandlingOptions, inference
from livekit.agents.voice.turn import EndpointingOptions
from livekit.plugins import elevenlabs


# Prefer variables supplied by Docker/the backend environment. The module
# .env file only fills values that are not already present.
load_dotenv(Path(__file__).with_name(".env"), override=False)

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


class VoiceLabAgent(Agent):
    def __init__(
        self,
        *,
        runtime_client: httpx.AsyncClient,
        session_id: str,
        room_name: str,
    ) -> None:
        super().__init__(instructions=AGENT_INSTRUCTIONS)
        self._runtime_client = runtime_client
        self._session_id = session_id
        self._room_name = room_name

    async def on_user_turn_completed(self, _turn_ctx, new_message) -> None:
        transcript = new_message.text_content
        if not isinstance(transcript, str) or not transcript.strip():
            self.session.say(
                "M\u00ecnh ch\u01b0a nghe r\u00f5 c\u00e2u tr\u1ea3 l\u1eddi. B\u1ea1n vui l\u00f2ng n\u00f3i l\u1ea1i nh\u00e9.",
                allow_interruptions=False,
            )
            return

        try:
            response = await self._runtime_client.post(
                "/ai/voice-lab/livekit/runtime/message",
                json={
                    "clientMessageId": f"livekit:{new_message.id}",
                    "content": transcript,
                    "telemetry": {
                        "roomName": self._room_name,
                        "transcriptConfidence": new_message.transcript_confidence,
                    },
                },
            )
            response.raise_for_status()
            assistant = response.json().get("assistantResponse")
            content = assistant.get("content") if isinstance(assistant, dict) else None
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("Interview Core returned no voice response")
        except Exception:
            logger.exception(
                "Interview Core voice turn failed",
                extra={"session_id": self._session_id, "room_name": self._room_name},
            )
            self.session.say(
                "Hi\u1ec7n t\u1ea1i m\u00ecnh ch\u01b0a x\u1eed l\u00fd \u0111\u01b0\u1ee3c c\u00e2u tr\u1ea3 l\u1eddi. B\u1ea1n vui l\u00f2ng th\u1eed l\u1ea1i nh\u00e9.",
                allow_interruptions=False,
            )
            return

        logger.info(
            "Interview Core voice turn completed",
            extra={"session_id": self._session_id, "room_name": self._room_name},
        )
        # Speak the deterministic response directly. Since llm=None, LiveKit skips
        # its automatic generation after this callback returns.
        self.session.say(content, allow_interruptions=False)


server = AgentServer()


@server.rtc_session(agent_name=os.getenv("LIVEKIT_AGENT_NAME", "intervia-voice"))
async def voice_lab_session(ctx: agents.JobContext) -> None:
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    if not voice_id:
        raise RuntimeError("ELEVENLABS_VOICE_ID is missing")

    stt_model = os.getenv("LIVEKIT_STT_MODEL", "assemblyai/universal-3-5-pro").strip()
    is_assemblyai = stt_model.startswith("assemblyai/")
    if is_assemblyai:
        # AssemblyAI Universal-3.5 Pro supports Vietnamese and automatic
        # Vietnamese/English code-switching when language is omitted.
        stt_language = os.getenv("LIVEKIT_STT_LANGUAGE", "auto").strip().lower()
        assemblyai_options: dict[str, object] = {
            "prompt": "Vietnamese IT interview with English technical terms.",
            "keyterms_prompt": [
                "REST API",
                "Docker",
                "Kubernetes",
                    "React",
                    "Next.js",
                    "NestJS",
                    "FastAPI",
                    "JWT",
                    "CI/CD",
                    "Cloudflare R2",
                    "vector database",
                    "Model Context Protocol",
                    "MCP",
                    "microservices",
                    "scalable system",
                    "high throughput",
                    "data consistency",
                ],
            # Keep natural pauses inside one answer instead of finalizing
            # every short clause as a separate turn.
            "mode": "balanced",
            "min_turn_silence": 700,
            "max_turn_silence": 2200,
            "previous_context_n_turns": 5,
        }
        if stt_language not in {"", "auto", "multi"}:
            assemblyai_options["language"] = stt_language
        stt = inference.STT(
            model=stt_model,
            extra_kwargs=assemblyai_options,
        )
    else:
        stt = inference.STT(
            model=stt_model,
            language=os.getenv("VOICE_LAB_STT_LANGUAGE", "vi"),
        )

    session = AgentSession(
        # Vietnamese mode is more reliable for short utterances. Set the env
        # value to "multi" only when the candidate frequently code-switches.
        stt=stt,
        # Raise the Silero activation threshold and require a short amount of
        # speech before opening a turn. This prevents air conditioners and
        # short background noises from keeping the turn open indefinitely.
        vad=inference.VAD(
            model="silero",
            min_speech_duration=_float_env("VOICE_LAB_VAD_MIN_SPEECH_DURATION", 0.15),
            min_silence_duration=_float_env("VOICE_LAB_VAD_MIN_SILENCE_DURATION", 1.30),
            activation_threshold=_float_env("VOICE_LAB_VAD_ACTIVATION_THRESHOLD", 0.62),
            deactivation_threshold=_float_env("VOICE_LAB_VAD_DEACTIVATION_THRESHOLD", 0.45),
        ),
        # Turn callback forwards transcripts to Interview Core; no free-form LLM is attached.
        llm=None,
        tts=elevenlabs.TTS(
            voice_id=voice_id,
            model=os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5"),
            language="vi",
        ),
        # End a user turn after a short, stable silence. This makes the lab
        # predictable for measurement and prevents waiting for manual input.
        turn_handling=TurnHandlingOptions(
            turn_detection="stt" if is_assemblyai else "vad",
            endpointing=EndpointingOptions(
                mode="fixed",
                min_delay=0.0 if is_assemblyai else 1.6,
                max_delay=2.8,
            ),
        ),
    )

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
        opening = start_response.json().get("openingMessage")
        opening_text = opening.get("content") if isinstance(opening, dict) else None
        session_id = str(start_response.json().get("sessionId") or "")
        if not opening_text or not session_id:
            raise RuntimeError("Interview Core returned no opening question")
    except Exception:
        await runtime_client.aclose()
        raise

    async def close_runtime_client(_reason: str) -> None:
        await runtime_client.aclose()

    ctx.add_shutdown_callback(close_runtime_client)
    agent = VoiceLabAgent(
        runtime_client=runtime_client,
        session_id=session_id,
        room_name=ctx.room.name,
    )
    await session.start(room=ctx.room, agent=agent)
    await session.say(opening_text, allow_interruptions=False)


if __name__ == "__main__":
    agents.cli.run_app(server)
