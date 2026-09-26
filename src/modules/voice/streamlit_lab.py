"""Standalone Streamlit voice lab for comparing realtime speech vs cascaded voice.

This file is intentionally isolated from Interview Core, DB, and question bank.
Run from repository root:
    streamlit run src/modules/voice/streamlit_lab.py
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import httpx
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

# Load the Voice Lab's own environment file.  Streamlit does not load .env
# files automatically, and this file must win over a stale shell variable when
# the model is changed during local testing.
load_dotenv(Path(__file__).with_name(".env"), override=True)

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
ELEVENLABS_BASE_URL = os.getenv("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io/v1")


@dataclass
class LatencyResult:
    name: str
    elapsed_ms: int
    detail: str = ""


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _openai_headers() -> dict[str, str]:
    key = _env("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is missing")
    return {"Authorization": f"Bearer {key}"}


def _eleven_headers() -> dict[str, str]:
    key = _env("ELEVENLABS_API_KEY")
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY is missing")
    return {"xi-api-key": key, "Content-Type": "application/json"}


def transcribe_openai(audio_bytes: bytes, filename: str, mime_type: str) -> tuple[str, LatencyResult]:
    started = time.perf_counter()
    model = _env("VOICE_LAB_STT_MODEL") or "gpt-4o-mini-transcribe"
    with httpx.Client(timeout=90) as client:
        r = client.post(
            f"{OPENAI_BASE_URL}/audio/transcriptions",
            headers=_openai_headers(),
            data={"model": model},
            files={"file": (filename, audio_bytes, mime_type)},
        )
        r.raise_for_status()
    elapsed = round((time.perf_counter() - started) * 1000)
    text = r.json().get("text", "")
    return text, LatencyResult("STT", elapsed, model)


def chat_openai(transcript: str, system_prompt: str) -> tuple[str, LatencyResult]:
    started = time.perf_counter()
    model = _env("VOICE_LAB_LLM_MODEL") or "gpt-5.6-mini"
    payload: dict[str, Any] = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": transcript}]},
        ],
    }
    with httpx.Client(timeout=90) as client:
        r = client.post(
            f"{OPENAI_BASE_URL}/responses",
            headers={**_openai_headers(), "Content-Type": "application/json"},
            json=payload,
        )
        if r.is_error:
            # Show the API's explanation in Streamlit while preserving the
            # original HTTPStatusError and its traceback for debugging.
            st.error(f"OpenAI API trả về HTTP {r.status_code}: {r.text}")
        r.raise_for_status()
    data = r.json()
    text = data.get("output_text", "")
    if not text:
        chunks: list[str] = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    chunks.append(content.get("text", ""))
        text = "".join(chunks)
    elapsed = round((time.perf_counter() - started) * 1000)
    return text, LatencyResult("LLM", elapsed, model)


def chat_openai_stream(
    transcript: str,
    system_prompt: str,
    on_delta: Callable[[str], None] | None = None,
) -> tuple[str, LatencyResult, int | None]:
    """Stream Responses API text deltas and return the completed text.

    The returned third value is time-to-first-token in milliseconds. The
    callback is intentionally optional so this function remains usable by
    non-UI callers.
    """
    started = time.perf_counter()
    first_token_ms: int | None = None
    model = _env("VOICE_LAB_LLM_MODEL") or "gpt-5-mini"
    payload: dict[str, Any] = {
        "model": model,
        "stream": True,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": transcript}]},
        ],
    }
    chunks: list[str] = []
    with httpx.Client(timeout=90) as client:
        with client.stream(
            "POST",
            f"{OPENAI_BASE_URL}/responses",
            headers={**_openai_headers(), "Content-Type": "application/json"},
            json=payload,
        ) as r:
            if r.is_error:
                body = r.read().decode("utf-8", errors="replace")
                st.error(f"OpenAI API trả về HTTP {r.status_code}: {body}")
            r.raise_for_status()
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                raw_event = line[5:].strip()
                if raw_event == "[DONE]":
                    break
                try:
                    event = json.loads(raw_event)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "error":
                    raise RuntimeError(event.get("message") or str(event))
                delta = event.get("delta", "") if event.get("type") == "response.output_text.delta" else ""
                if not delta:
                    continue
                if first_token_ms is None:
                    first_token_ms = round((time.perf_counter() - started) * 1000)
                chunks.append(delta)
                if on_delta:
                    on_delta(delta)
    elapsed = round((time.perf_counter() - started) * 1000)
    return "".join(chunks), LatencyResult("LLM", elapsed, model), first_token_ms


def tts_elevenlabs(text: str) -> tuple[bytes, LatencyResult]:
    started = time.perf_counter()
    voice_id = _env("ELEVENLABS_VOICE_ID")
    if not voice_id:
        raise RuntimeError("ELEVENLABS_VOICE_ID is missing")
    model_id = _env("ELEVENLABS_MODEL_ID") or "eleven_flash_v2_5"
    with httpx.Client(timeout=90) as client:
        r = client.post(
            f"{ELEVENLABS_BASE_URL}/text-to-speech/{voice_id}",
            headers=_eleven_headers(),
            params={"output_format": _env("ELEVENLABS_OUTPUT_FORMAT") or "mp3_44100_128"},
            json={
                "text": text,
                "model_id": model_id,
                "voice_settings": {
                    "stability": float(_env("ELEVENLABS_STABILITY") or "0.5"),
                    "similarity_boost": float(_env("ELEVENLABS_SIMILARITY_BOOST") or "0.75"),
                },
            },
        )
        if r.is_error:
            st.error(f"ElevenLabs API trả về HTTP {r.status_code}: {r.text}")
        r.raise_for_status()
    elapsed = round((time.perf_counter() - started) * 1000)
    return r.content, LatencyResult("TTS", elapsed, model_id)


def tts_elevenlabs_stream(text: str) -> tuple[bytes, LatencyResult, int | None]:
    """Read ElevenLabs audio from its streaming endpoint as it is generated."""
    started = time.perf_counter()
    first_audio_ms: int | None = None
    voice_id = _env("ELEVENLABS_VOICE_ID")
    if not voice_id:
        raise RuntimeError("ELEVENLABS_VOICE_ID is missing")
    model_id = _env("ELEVENLABS_MODEL_ID") or "eleven_flash_v2_5"
    audio_chunks: list[bytes] = []
    with httpx.Client(timeout=90) as client:
        with client.stream(
            "POST",
            f"{ELEVENLABS_BASE_URL}/text-to-speech/{voice_id}/stream",
            headers=_eleven_headers(),
            params={"output_format": _env("ELEVENLABS_OUTPUT_FORMAT") or "mp3_44100_128"},
            json={
                "text": text,
                "model_id": model_id,
                "voice_settings": {
                    "stability": float(_env("ELEVENLABS_STABILITY") or "0.5"),
                    "similarity_boost": float(_env("ELEVENLABS_SIMILARITY_BOOST") or "0.75"),
                },
            },
        ) as r:
            if r.is_error:
                body = r.read().decode("utf-8", errors="replace")
                st.error(f"ElevenLabs API trả về HTTP {r.status_code}: {body}")
            r.raise_for_status()
            for chunk in r.iter_bytes():
                if not chunk:
                    continue
                if first_audio_ms is None:
                    first_audio_ms = round((time.perf_counter() - started) * 1000)
                audio_chunks.append(chunk)
    elapsed = round((time.perf_counter() - started) * 1000)
    return b"".join(audio_chunks), LatencyResult("TTS", elapsed, model_id), first_audio_ms


def create_realtime_client_secret(system_prompt: str) -> tuple[dict[str, Any], LatencyResult]:
    started = time.perf_counter()
    model = _env("VOICE_LAB_REALTIME_MODEL") or "gpt-realtime"
    voice = _env("VOICE_LAB_REALTIME_VOICE") or "marin"
    payload = {
        "session": {
            "type": "realtime",
            "model": model,
            "instructions": system_prompt,
            "audio": {"output": {"voice": voice}},
        }
    }
    with httpx.Client(timeout=45) as client:
        r = client.post(
            f"{OPENAI_BASE_URL}/realtime/client_secrets",
            headers={**_openai_headers(), "Content-Type": "application/json"},
            json=payload,
        )
        r.raise_for_status()
    elapsed = round((time.perf_counter() - started) * 1000)
    return r.json(), LatencyResult("Realtime secret", elapsed, f"{model} / {voice}")


def create_livekit_join_token(room_name: str) -> tuple[str, str]:
    """Create a short-lived browser token with explicit agent dispatch."""
    livekit_url = _env("LIVEKIT_URL")
    api_key = _env("LIVEKIT_API_KEY")
    api_secret = _env("LIVEKIT_API_SECRET")
    if not livekit_url or not api_key or not api_secret:
        raise RuntimeError("LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required")
    try:
        from livekit import api
    except ImportError as exc:
        raise RuntimeError(
            'Install LiveKit dependencies first: python -m pip install -e ".[voice-realtime]"'
        ) from exc

    agent_name = _env("LIVEKIT_AGENT_NAME") or "intervia-voice"
    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity(f"streamlit-{uuid4().hex[:12]}")
        .with_name("Voice Lab candidate")
        .with_ttl(timedelta(minutes=10))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=agent_name)]))
        .to_jwt()
    )
    return livekit_url, token


def render_livekit_client(server_url: str, token: str) -> None:
    """Render a small browser client so the standalone Streamlit lab can join LiveKit."""
    server_json = json.dumps(server_url)
    token_json = json.dumps(token)
    components.html(
        f"""
        <script src="https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js"></script>
        <style>
          body {{ font-family: sans-serif; }}
          button {{ margin-right: 8px; padding: 6px 12px; }}
          #turnAction {{ color: white; border: 0; border-radius: 5px; font-weight: 600; }}
          #turnAction.green {{ background: #16a34a; }}
          #turnAction.red {{ background: #dc2626; }}
          #turnAction.yellow {{ background: #eab308; color: #111827; }}
          #turnAction.gray {{ background: #6b7280; }}
          #status {{ margin: 10px 0; color: #555; }}
          #transcript {{ white-space: pre-wrap; max-height: 140px; overflow: auto; }}
        </style>
        <button id="connect">Connect & start test</button>
        <button id="disconnect" disabled>End conversation</button>
        <button id="turnAction" class="gray" disabled>Waiting for connection</button>
        <div id="status">Ready</div>
        <pre id="metrics"></pre>
        <div id="transcript"></div>
        <script>
          const url = {server_json};
          const token = {token_json};
          const status = document.getElementById("status");
          const metrics = document.getElementById("metrics");
          const transcript = document.getElementById("transcript");
          let room = null;
          let agentSeen = false;
          let agentAudioReady = false;
          let userTurnStarted = false;
          let agentSpeakingNow = false;
          let wasAgentSpeaking = false;
          let wasUserSpeaking = false;
          let localMicTrackSid = null;
          let agentAudioTrackSid = null;
          let pendingTurns = [];
          let turnCount = 0;
          const seenSegments = new Set();
          const turnAction = document.getElementById("turnAction");
          let startedAt = null;
          const timings = {{}};
          function setStatus(value) {{ status.textContent = value; }}
          function setTurnState(color, label, enabled) {{
            turnAction.className = color;
            turnAction.textContent = label;
            turnAction.disabled = !enabled;
          }}
          function mark(name) {{
            if (startedAt === null) return;
            if (timings[name] === undefined) timings[name] = Math.round(performance.now() - startedAt);
            metrics.textContent = Object.entries(timings).map(([key, value]) => key + ": " + value + " ms").join("\\n");
          }}
          function attachTrack(track, publication, participant) {{
            if (track.kind === "audio") {{
              document.body.appendChild(track.attach());
              if (participant && (!room || participant.identity !== room.localParticipant.identity)) {{
                agentAudioTrackSid = publication && (publication.trackSid || publication.sid);
                agentAudioReady = true;
              }}
              mark("agent audio track");
              setStatus("Agent audio track connected; speak after the greeting");
            }}
          }}
          document.getElementById("connect").onclick = async () => {{
            try {{
              startedAt = performance.now();
              for (const key of Object.keys(timings)) delete timings[key];
              agentSeen = false;
              agentAudioReady = false;
              userTurnStarted = false;
              agentSpeakingNow = false;
              wasAgentSpeaking = false;
              wasUserSpeaking = false;
              localMicTrackSid = null;
              agentAudioTrackSid = null;
              pendingTurns = [];
              turnCount = 0;
              seenSegments.clear();
              transcript.textContent = "";
              mark("connect start");
              room = new LivekitClient.Room();
              room.on(LivekitClient.RoomEvent.TrackSubscribed, attachTrack);
              room.on(LivekitClient.RoomEvent.ActiveSpeakersChanged, (speakers) => {{
                const userSpeaking = speakers.some((participant) => participant.identity === room.localParticipant.identity);
                const agentSpeaking = speakers.some((participant) => participant.identity !== room.localParticipant.identity);
                const agentStartedSpeaking = agentSpeaking && !wasAgentSpeaking;
                const userStartedSpeaking = userSpeaking && !wasUserSpeaking;
                agentSpeakingNow = agentSpeaking;
                wasAgentSpeaking = agentSpeaking;
                wasUserSpeaking = userSpeaking;
                if (agentAudioReady && userStartedSpeaking && agentSpeaking) {{
                  setTurnState("yellow", "Interrupted — wait for AI", false);
                  mark("interruption");
                }} else if (agentSpeaking) {{
                  setTurnState("red", "AI is speaking", false);
                }} else if (agentAudioReady && userSpeaking) {{
                  setTurnState("green", "Your turn — speaking", true);
                  if (!userTurnStarted) mark("user turn start");
                }} else if (agentAudioReady && agentSeen && pendingTurns.length === 0) {{
                  setTurnState("green", "Your turn — speak now", true);
                }}
                if (agentAudioReady && userSpeaking) {{
                  userTurnStarted = true;
                  setStatus("Your turn is active; finish speaking and pause");
                }}
                if (agentStartedSpeaking && pendingTurns.length > 0) {{
                  const responseTurn = pendingTurns.shift();
                  mark("first response speech");
                  mark("turn " + responseTurn + " response speech");
                  setStatus("AI response for turn " + responseTurn + " is streaming");
                }}
              }});
              room.on(LivekitClient.RoomEvent.ParticipantConnected, (participant) => {{
                if (participant.identity !== room.localParticipant.identity) {{
                  agentSeen = true;
                  mark("agent joined");
                  setStatus("Agent joined; waiting for microphone response");
                }}
              }});
              if (LivekitClient.RoomEvent.TranscriptionReceived) {{
                room.on(LivekitClient.RoomEvent.TranscriptionReceived, (segments, participant) => {{
                  for (const segment of segments) {{
                    const transcribedTrackId = segment.transcribedTrackId || segment.trackId || null;
                    const isLocalTranscript = participant &&
                      participant.identity === room.localParticipant.identity;
                    const isRemoteTranscript = participant &&
                      participant.identity !== room.localParticipant.identity;
                    // LiveKit agents usually publish both candidate STT and agent
                    // TTS transcripts from the remote agent participant. The
                    // transcribed track id is therefore the reliable source of
                    // who actually spoke; the participant alone is not enough.
                    const isMicTranscript = localMicTrackSid &&
                      transcribedTrackId === localMicTrackSid;
                    const isAgentTranscript = agentAudioTrackSid &&
                      transcribedTrackId === agentAudioTrackSid;
                    const isUserTranscript = userTurnStarted &&
                      (isMicTranscript || isLocalTranscript ||
                       (!isAgentTranscript && !agentSpeakingNow && (isRemoteTranscript || !participant)));
                    if (segment.text && segment.final !== false && isUserTranscript) {{
                      const segmentKey = [transcribedTrackId || "unknown",
                        segment.id || segment.segmentId || segment.startTime || segment.text].join(":");
                      if (seenSegments.has(segmentKey)) continue;
                      seenSegments.add(segmentKey);
                      turnCount += 1;
                      pendingTurns.push(turnCount);
                      userTurnStarted = false;
                      mark("first transcript");
                      mark("turn " + turnCount + " transcript");
                      transcript.textContent += "Turn " + turnCount + " — You: " + segment.text + "\\n";
                      setStatus("Turn " + turnCount + " received; waiting for AI");
                    }} else if (segment.text && segment.final !== false &&
                               (isAgentTranscript || isRemoteTranscript || agentSpeakingNow)) {{
                      transcript.textContent += "AI: " + segment.text + "\\n";
                    }}
                  }}
                }});
              }}
              await room.connect(url, token);
              mark("room connected");
              await room.localParticipant.setMicrophoneEnabled(true, {{
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
              }});
              const micPublication = room.localParticipant.getTrackPublication(
                LivekitClient.Track.Source.Microphone
              );
              localMicTrackSid = micPublication &&
                (micPublication.trackSid || micPublication.sid);
              setStatus("Connected; microphone is streaming; waiting for agent");
              setTurnState("gray", "Waiting for agent", false);
              setTimeout(() => {{
                if (!agentSeen) setStatus("No agent after 15 s. Check the agent terminal and use a new room.");
              }}, 15000);
              document.getElementById("connect").disabled = true;
              document.getElementById("disconnect").disabled = false;
              turnAction.onclick = () => {{
                if (!turnAction.disabled) {{
                  mark("user turn start");
                  setStatus("Microphone is listening; speak now");
                }}
              }};
            }} catch (error) {{
              setStatus("Connection failed: " + error.message);
              setTurnState("yellow", "Connection error", false);
              mark("connection failed");
            }}
          }};
          document.getElementById("disconnect").onclick = async () => {{
            if (room) await room.disconnect();
            room = null;
            setStatus("Conversation ended after " + turnCount + " user turns");
            setTurnState("gray", "Conversation ended", false);
            document.getElementById("connect").disabled = false;
            document.getElementById("disconnect").disabled = true;
          }};
        </script>
        """,
        height=300,
    )


DEFAULT_PROMPT = """You are a professional Vietnamese IT interviewer.
Vietnamese is the primary conversational language.
The candidate may naturally mix Vietnamese and English technical terminology.
Keep common IT terms such as API, Docker, Kubernetes, React, Next.js, NestJS, JWT,
CI/CD, dependency injection and race condition in English.
Ask only one concise interview question at a time.
If the candidate's meaning is genuinely unclear, ask one short clarification question.
Do not score the candidate and do not reveal evaluation criteria.
Keep your speaking style calm, professional, neutral and concise. Hỏi từng câu hỏi không hỏi 1 lần nhiều câu"""


st.set_page_config(page_title="Voice Lab", page_icon="🎙️", layout="wide")
st.title("🎙️ INTERVIA Voice Lab")
st.caption("Standalone experiment only — no DB writes, no Question Bank, no Interview Core mutation.")

with st.sidebar:
    st.subheader("Environment")
    env_rows = {
        "OPENAI_API_KEY": bool(_env("OPENAI_API_KEY")),
        "VOICE_LAB_STT_LANGUAGE": bool(_env("VOICE_LAB_STT_LANGUAGE")),
        "ELEVENLABS_API_KEY": bool(_env("ELEVENLABS_API_KEY")),
        "ELEVENLABS_VOICE_ID": bool(_env("ELEVENLABS_VOICE_ID")),
        "LIVEKIT_URL": bool(_env("LIVEKIT_URL")),
        "LIVEKIT_API_KEY": bool(_env("LIVEKIT_API_KEY")),
        "LIVEKIT_API_SECRET": bool(_env("LIVEKIT_API_SECRET")),
    }
    for key, ok in env_rows.items():
        st.write(("✅" if ok else "⬜") + " " + key)

system_prompt = st.text_area("System prompt", DEFAULT_PROMPT, height=230)

tab2, tab3 = st.tabs([
    "LiveKit Realtime Speech",
    "Latency notes",
])

with tab2:
    st.subheader("LiveKit multi-turn conversation test")
    st.write(
        "Mỗi lần bấm Start tạo một room mới. Sau khi Connect, agent chào rồi giữ ngữ cảnh "
        "qua nhiều lượt: Bạn → AI → Bạn → AI. Bảng đo ghi từng transcript và thời điểm AI "
        "bắt đầu trả lời mỗi lượt."
    )
    room_name = st.text_input("Test room prefix", value="voice-lab")
    if st.button("Start LiveKit streaming", type="primary"):
        try:
            prefix = room_name.strip() or "voice-lab"
            test_room = f"{prefix}-{uuid4().hex[:8]}"
            server_url, token = create_livekit_join_token(test_room)
            st.success(f"New test room created: {test_room}")
            render_livekit_client(server_url, token)
        except Exception as exc:
            st.exception(exc)

    st.caption(
        "Start the LiveKit agent in another terminal first. Each test uses a new room "
        "so the agent dispatch is applied: python src/modules/voice/livekit_agent.py dev"
    )
    st.caption("Màu xanh: bạn có thể nói · màu đỏ: AI đang nói · màu vàng: bạn vừa ngắt lời AI")
    st.divider()
    st.subheader("OpenAI Realtime configuration check")
    if st.button("Create OpenAI Realtime client secret"):
        try:
            secret, metric = create_realtime_client_secret(system_prompt)
            st.success(f"Realtime session config accepted in {metric.elapsed_ms} ms")
            safe = {
                "model": secret.get("session", {}).get("model") or _env("VOICE_LAB_REALTIME_MODEL") or "gpt-realtime",
                "voice": _env("VOICE_LAB_REALTIME_VOICE") or "marin",
                "has_client_secret": bool(secret.get("value") or secret.get("client_secret")),
            }
            st.json(safe)
            st.warning("The actual ephemeral secret is intentionally not rendered in the UI.")
        except Exception as exc:
            st.exception(exc)

    st.code(
        """Candidate Mic
   ├──> Native Realtime Speech ──> interviewer audio
   └──> Observer/STT ──> transcript/evidence/scoring

Observer must never block the realtime speech path.""",
        language="text",
    )

with tab3:
    st.subheader("What this prototype measures")
    st.write(
        "Use identical Vietnamese–English IT answers for both providers. "
        "Record STT accuracy for technical terms, interviewer instruction-following, "
        "clarification behavior, voice naturalness, and end-of-turn → first-audio latency."
    )
    st.write(
        "For production-grade latency, add timestamps for: candidate_last_audio, end_of_turn, "
        "stt_final, llm_first_token, tts_first_audio, candidate_hears_audio."
    )
