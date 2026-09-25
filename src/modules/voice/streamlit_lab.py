"""Standalone Streamlit voice lab for comparing realtime speech vs cascaded voice.

This file is intentionally isolated from Interview Core, DB, and question bank.
Run from repository root:
    streamlit run src/modules/voice/streamlit_lab.py
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
import streamlit as st

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
        r.raise_for_status()
    elapsed = round((time.perf_counter() - started) * 1000)
    return r.content, LatencyResult("TTS", elapsed, model_id)


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


DEFAULT_PROMPT = """You are a professional Vietnamese IT interviewer.
Vietnamese is the primary conversational language.
The candidate may naturally mix Vietnamese and English technical terminology.
Keep common IT terms such as API, Docker, Kubernetes, React, Next.js, NestJS, JWT,
CI/CD, dependency injection and race condition in English.
Ask only one concise interview question at a time.
If the candidate's meaning is genuinely unclear, ask one short clarification question.
Do not score the candidate and do not reveal evaluation criteria.
Keep your speaking style calm, professional, neutral and concise."""


st.set_page_config(page_title="Voice Lab", page_icon="🎙️", layout="wide")
st.title("🎙️ INTERVIA Voice Lab")
st.caption("Standalone experiment only — no DB writes, no Question Bank, no Interview Core mutation.")

with st.sidebar:
    st.subheader("Environment")
    env_rows = {
        "OPENAI_API_KEY": bool(_env("OPENAI_API_KEY")),
        "ELEVENLABS_API_KEY": bool(_env("ELEVENLABS_API_KEY")),
        "ELEVENLABS_VOICE_ID": bool(_env("ELEVENLABS_VOICE_ID")),
        "LIVEKIT_URL": bool(_env("LIVEKIT_URL")),
        "LIVEKIT_API_KEY": bool(_env("LIVEKIT_API_KEY")),
        "LIVEKIT_API_SECRET": bool(_env("LIVEKIT_API_SECRET")),
    }
    for key, ok in env_rows.items():
        st.write(("✅" if ok else "⬜") + " " + key)

system_prompt = st.text_area("System prompt", DEFAULT_PROMPT, height=230)

tab1, tab2, tab3 = st.tabs([
    "A — Cascaded STT → LLM → TTS",
    "B — Native Realtime Speech",
    "Latency notes",
])

with tab1:
    st.subheader("Cascaded lab")
    audio = st.audio_input("Record a candidate answer")
    if audio is not None:
        st.audio(audio)
        if st.button("Run cascaded pipeline", type="primary"):
            try:
                raw = audio.getvalue()
                transcript, stt_metric = transcribe_openai(raw, audio.name or "candidate.wav", audio.type or "audio/wav")
                reply, llm_metric = chat_openai(transcript, system_prompt)
                speech, tts_metric = tts_elevenlabs(reply)
                total = stt_metric.elapsed_ms + llm_metric.elapsed_ms + tts_metric.elapsed_ms

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("STT", f"{stt_metric.elapsed_ms} ms")
                c2.metric("LLM", f"{llm_metric.elapsed_ms} ms")
                c3.metric("TTS", f"{tts_metric.elapsed_ms} ms")
                c4.metric("Sequential total", f"{total} ms")

                st.markdown("**Transcript**")
                st.write(transcript)
                st.markdown("**Interviewer response**")
                st.write(reply)
                st.audio(speech, format="audio/mp3")
                st.info(
                    "This first lab measures sequential wall-clock latency. "
                    "It intentionally does not claim production streaming TTFA yet."
                )
            except Exception as exc:
                st.exception(exc)

with tab2:
    st.subheader("Native realtime speech setup")
    st.write(
        "This tab validates the realtime model/session configuration first. "
        "Browser WebRTC/LiveKit wiring can be added next without touching Interview Core."
    )
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
