"""LiveKit streaming voice agent for the standalone Voice Lab.

Run from the repository root after installing the optional dependency group:

    python -m pip install -e ".[voice-realtime]"
    python src/modules/voice/livekit_agent.py dev

The media path is fully streaming:

    LiveKit audio -> Deepgram Nova-3 Vietnamese STT
    -> OpenAI streaming LLM -> ElevenLabs streaming TTS -> LiveKit audio

The agent is intentionally isolated from Interview Core persistence and the
question bank. Transcripts are published by LiveKit to the connected client.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, TurnHandlingOptions, inference
from livekit.agents.voice.turn import EndpointingOptions
from livekit.plugins import elevenlabs, openai


load_dotenv(Path(__file__).with_name(".env"), override=True)

# The ElevenLabs plugin uses ELEVEN_API_KEY, while the existing Voice Lab uses
# the more explicit ELEVENLABS_API_KEY name.
if os.getenv("ELEVENLABS_API_KEY") and not os.getenv("ELEVEN_API_KEY"):
    os.environ["ELEVEN_API_KEY"] = os.environ["ELEVENLABS_API_KEY"]


SYSTEM_PROMPT = """You are a professional Vietnamese IT interviewer.
Vietnamese is the primary conversational language.
The candidate may naturally mix Vietnamese and English technical terminology.
Keep common IT terms such as API, Docker, Kubernetes, React, Next.js, NestJS,
JWT, CI/CD, dependency injection and race condition in English.
Ask only one concise interview question at a time.
If the candidate's meaning is genuinely unclear, ask one short clarification question.
Do not score the candidate and do not reveal evaluation criteria.
Keep your speaking style calm, professional, neutral and concise."""


def _float_env(name: str, default: float) -> float:
    """Read a numeric tuning value without making a bad .env stop the agent."""
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


class VoiceLabAgent(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=SYSTEM_PROMPT)


server = AgentServer()


@server.rtc_session(agent_name=os.getenv("LIVEKIT_AGENT_NAME", "intervia-voice"))
async def voice_lab_session(ctx: agents.JobContext) -> None:
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    if not voice_id:
        raise RuntimeError("ELEVENLABS_VOICE_ID is missing")

    session = AgentSession(
        # Vietnamese is the primary language. Set VOICE_LAB_STT_LANGUAGE=multi
        # when testing automatic Vietnamese/English code-switch detection.
        stt=inference.STT(
            model="deepgram/nova-3",
            language=os.getenv("VOICE_LAB_STT_LANGUAGE", "vi"),
        ),
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
        llm=openai.LLM(model=os.getenv("VOICE_LAB_LLM_MODEL", "gpt-5-mini")),
        tts=elevenlabs.TTS(
            voice_id=voice_id,
            model=os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5"),
            language="vi",
        ),
        # End a user turn after a short, stable silence. This makes the lab
        # predictable for measurement and prevents waiting for manual input.
        turn_handling=TurnHandlingOptions(
            turn_detection="vad",
            endpointing=EndpointingOptions(mode="fixed", min_delay=1.6, max_delay=2.8),
        ),
    )

    await session.start(room=ctx.room, agent=VoiceLabAgent())
    await session.generate_reply(
        instructions="Greet the candidate briefly in Vietnamese and ask the first interview question."
    )


if __name__ == "__main__":
    agents.cli.run_app(server)
