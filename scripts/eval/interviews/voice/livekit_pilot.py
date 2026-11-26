"""Pilot benchmark driver: a synthetic candidate in a real LiveKit room (V2 §13).

Plays pre-recorded candidate answers (``livekit_benchmark.py`` describes the
four configurations; the answers are the same WAV files for every run) into
a real interview session -- real room, real voice agent, real Qwen STT, real
Thinker, real VieNeu TTS -- and lets the production endpointing decide when
each answer is over. Nothing is simulated except the mouth.

What it does, in order:

1. mints a user token with the API's own secret (a pilot-only shortcut for a
   Google-authenticated dev account), creates a voice session for the given
   CV and job, runs P1/P2 (plan, select), and asks for a LiveKit room token
   exactly as the web client does;
2. joins the room as the candidate, publishes a 16 kHz microphone track that
   carries silence whenever it is not speaking (the agent's VAD needs a live
   track to hear the end of an answer), and subscribes to the agent's audio;
3. speaks the next answer when the agent has finished its question -- the
   agent's ``turn.state == listening`` message *and* 1.5 s of silence on its
   audio track -- with a short natural pause first;
4. measures on the client what the server cannot: the time from the end of
   the candidate's audio to the first audible agent audio (client TTFA);
5. stops after N answers, the session closing, a per-turn timeout, or the
   per-session provider-call budget (read from ``ai_call_logs``) being hit;
6. closes the session, then writes a JSON record: session id, the **actual**
   feature flags read from the running backend and voice-agent containers,
   provider and model names from the call log and the voice events, the
   server-side §13 metrics (``livekit_turn_metrics``) and the client-side ones.

Eight turns is a pilot. The record says so; nothing here is a P95.

Run (per configuration; see ``livekit_benchmark.py`` for the flags):

    python scripts/eval/interviews/voice/livekit_pilot.py --config anticipation \\
        --user <user id> --email <email> --resume <cv id> --job <job id> \\
        --audio-dir <dir with answer_NN.wav> --turns 8 --budget-calls 40 --out <record.json>
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import struct
import subprocess
import sys
import time
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt

SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
SILENCE = bytes(FRAME_SAMPLES * 2)
#: int16 RMS above which a 20 ms agent frame counts as speech.
LOUD_RMS = 400.0
#: The agent has finished its question when its track has been quiet this
#: long. VieNeu synthesizes sentence by sentence with a first byte of about a
#: second, so a gap inside one question can exceed 1.5 s; 3 s tells the end of
#: the question from a pause within it (a human would read the intonation).
AGENT_SILENCE_S = 3.0
#: Natural pause before the candidate starts answering.
PAUSE_BEFORE_ANSWER_S = 0.9
API_INTERVIEWS = "/api/v1/interviews"
API_VOICE = "/ai/voice-lab"
FLAG_NAMES = (
    "PILOT_CONFIG", "INTERVIEW_UNIFIED_THINKER", "VOICE_LLM_SPECULATIVE_PREVIEW",
    "VOICE_ADAPTIVE_ENDPOINTING", "INTERVIEW_TALKER_ENABLED", "INTERVIEW_AGENT_MODE",
    "INTERVIEW_BACKCHANNEL_ENABLED", "INTERVIEW_TALKER_MODEL", "VOICE_ANSWER_COMMIT_ANCHOR",
    "VOICE_ANSWER_PAUSE_NUDGE_ENABLED",
    "LIVEKIT_STT_PROVIDER", "QWEN_STT_MODEL", "VOICE_LAB_TTS_PROVIDER", "VIENEU_TTS_MODEL",
    "VIENEU_TTS_VOICE", "LLM_MODEL",
)


def _rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    return (sum(s * s for s in samples) / n) ** 0.5


def _load_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SAMPLE_RATE and w.getnchannels() == 1 and w.getsampwidth() == 2, path
        return w.readframes(w.getnframes())


def _metrics_module():
    path = Path(__file__).with_name("livekit_turn_metrics.py")
    spec = importlib.util.spec_from_file_location("livekit_turn_metrics", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("livekit_turn_metrics", module)
    spec.loader.exec_module(module)
    return module


def container_flags(compose_files: list[str]) -> dict[str, dict[str, str]]:
    """The flags as the *running* backend and voice-agent containers see them."""
    out: dict[str, dict[str, str]] = {}
    for service in ("backend", "voice-agent"):
        # `env`, not `printenv NAME...`: printenv exits non-zero and prints
        # nothing useful when any one name is unset in the container.
        cmd = ["docker", "compose", *sum((["-f", f] for f in compose_files), []), "exec", "-T", service, "env"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
            values = {
                k: v for k, v in (line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)
                if k in FLAG_NAMES
            }
            if not values:
                values = {"error": (res.stderr or "no output")[:200]}
        except Exception as exc:  # noqa: BLE001 - recorded, not fatal
            values = {"error": str(exc)[:120]}
        out[service] = values
    return out


# ------------------------------------------------------------------- API


class Api:
    def __init__(self, base: str, token: str) -> None:
        self.client = httpx.AsyncClient(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=180)

    async def post(self, path: str, body: dict | None = None) -> dict:
        r = await self.client.post(path, json=body or {})
        if r.status_code >= 400:
            raise SystemExit(f"{path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    async def close(self) -> None:
        await self.client.aclose()


def mint_token(secret: str, algorithm: str, user_id: str, email: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode({"sub": user_id, "email": email, "iat": now, "exp": now + timedelta(hours=2)}, secret, algorithm=algorithm)


# ------------------------------------------------------------- candidate


class Candidate:
    """The synthetic candidate: one microphone track, ears on the agent."""

    def __init__(self, answers: list[bytes], *, turns: int, budget_calls: int, call_counter, log) -> None:
        from livekit import rtc

        self.rtc = rtc
        self.answers = answers[:turns]
        self.budget_calls = budget_calls
        self.call_counter = call_counter
        self.log = log
        self.room = rtc.Room()
        self.source = rtc.AudioSource(SAMPLE_RATE, 1)
        self.to_say: asyncio.Queue[bytes] = asyncio.Queue()
        self.state = "unknown"
        self.state_at: dict[str, float] = {}
        self.agent_last_loud: float | None = None
        self.agent_spoke_since_answer = False
        self.agent_seen = False
        self.session_closed = False
        self.turns: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.stop_reason: str | None = None
        self._speaking = False
        self._mic_task: asyncio.Task | None = None
        self._ear_tasks: list[asyncio.Task] = []

    # -- room wiring ---------------------------------------------------------

    async def connect(self, url: str, token: str) -> None:
        rtc = self.rtc
        room = self.room

        @room.on("data_received")
        def on_data(packet) -> None:
            if getattr(packet, "topic", None) != "intervia.turn":
                return
            try:
                message = json.loads(bytes(packet.data).decode("utf-8"))
            except Exception:  # noqa: BLE001
                return
            kind = message.get("type")
            now = time.perf_counter()
            if kind == "turn.state":
                self.state = str(message.get("state"))
                self.state_at[self.state] = now
                if self.current is not None and self.state == "processing" and "processing_at" not in self.current:
                    self.current["processing_at"] = now
                if self.current is not None and self.state == "listening" and "listening_at" not in self.current:
                    self.current["listening_at"] = now
                self.log(f"turn.state={self.state}")
            elif kind == "voice.late_transcript":
                self.log("late transcript captured by the agent")
                if self.current is not None:
                    self.current["late_transcript"] = True
            elif kind in ("session.closed", "session.completed"):
                self.session_closed = True

        @room.on("participant_connected")
        def on_participant(participant) -> None:
            self.agent_seen = True
            self.log(f"participant joined: {participant.identity}")

        @room.on("track_subscribed")
        def on_track(track, publication, participant) -> None:
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                self.agent_seen = True
                self.log(f"agent audio track from {participant.identity}")
                self._ear_tasks.append(asyncio.create_task(self._listen(rtc.AudioStream(track))))

        @room.on("disconnected")
        def on_disconnected(*_a) -> None:
            self.log("room disconnected")
            self.session_closed = True

        await room.connect(url, token)
        track = rtc.LocalAudioTrack.create_audio_track("candidate-mic", self.source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        await room.local_participant.publish_track(track, options)
        self._mic_task = asyncio.create_task(self._microphone())

    async def _listen(self, stream) -> None:
        frames = 0
        loud = 0
        async for event in stream:
            frame = event.frame
            frames += 1
            level = _rms(bytes(frame.data))
            if frames == 1:
                self.log(f"agent audio: {frame.sample_rate} Hz, {frame.num_channels} ch, {frame.samples_per_channel} samples/frame")
            if level > LOUD_RMS:
                loud += 1
                now = time.perf_counter()
                previous_loud = self.agent_last_loud
                if self.agent_last_loud is None or now - self.agent_last_loud > AGENT_SILENCE_S:
                    self.log(f"agent audio started (rms {level:.0f})")
                self.agent_last_loud = now
                self.agent_spoke_since_answer = True
                current = self.current
                if (
                    current is not None
                    and "first_audio_at" not in current
                    and not self._speaking
                    and current.get("speech_end_at") is not None
                    and now > current["speech_end_at"]
                ):
                    current["first_audio_at"] = now
                # Client TTMR: the first audio after the agent said it is
                # listening again (turn.state after Core answered), with a
                # short quiet gap before it so a backchannel being cut at that
                # moment is not mistaken for the reply (its TTS leaves ~0.4 s).
                if (
                    current is not None
                    and "reply_audio_at" not in current
                    and current.get("listening_at") is not None
                    and now >= current["listening_at"]
                    and (previous_loud is None or now - previous_loud >= 0.15 or previous_loud < current["listening_at"])
                ):
                    current["reply_audio_at"] = now
            if frames % 500 == 0:
                self.log(f"agent audio: {frames} frames, {loud} loud, last rms {level:.0f}")

    async def _microphone(self) -> None:
        """Silence at real-time pace, or the queued answer. Never stops sending."""
        rtc = self.rtc
        while True:
            try:
                pcm = self.to_say.get_nowait()
            except asyncio.QueueEmpty:
                pcm = None
            if pcm is None:
                await self.source.capture_frame(rtc.AudioFrame(SILENCE, SAMPLE_RATE, 1, FRAME_SAMPLES))
                continue
            self._speaking = True
            for offset in range(0, len(pcm), FRAME_SAMPLES * 2):
                chunk = pcm[offset : offset + FRAME_SAMPLES * 2]
                if len(chunk) < FRAME_SAMPLES * 2:
                    chunk = chunk + bytes(FRAME_SAMPLES * 2 - len(chunk))
                await self.source.capture_frame(rtc.AudioFrame(chunk, SAMPLE_RATE, 1, FRAME_SAMPLES))
            self._speaking = False
            if self.current is not None:
                self.current["speech_end_at"] = time.perf_counter()
                self.agent_spoke_since_answer = False
            self.log("answer audio finished")

    # -- the interview -------------------------------------------------------

    def _agent_ready_for_answer(self) -> bool:
        # The agent publishes turn.state only from an answer onwards; before
        # the first answer the only signal is its audio going quiet after the
        # greeting and the first question.
        if self.state not in ("listening", "unknown") or not self.agent_spoke_since_answer or self.agent_last_loud is None:
            return False
        return (time.perf_counter() - self.agent_last_loud) >= AGENT_SILENCE_S

    async def run(self, *, turn_timeout_s: float = 150.0, session_timeout_s: float = 1500.0) -> str:
        started = time.perf_counter()
        for index, pcm in enumerate(self.answers, 1):
            waited = time.perf_counter()
            while not self._agent_ready_for_answer():
                if self.session_closed:
                    return self._stop("session_closed_before_turn")
                if time.perf_counter() - waited > turn_timeout_s:
                    return self._stop(f"timeout_waiting_for_question_{index}")
                if time.perf_counter() - started > session_timeout_s:
                    return self._stop("session_timeout")
                await asyncio.sleep(0.05)
            calls = await self.call_counter()
            if calls > self.budget_calls:
                return self._stop(f"budget_exhausted_{calls}_calls")
            await asyncio.sleep(PAUSE_BEFORE_ANSWER_S)
            self.current = {"turn": index, "calls_before": calls, "question_ended_at": self.agent_last_loud}
            self.turns.append(self.current)
            self.log(f"answer {index}: speaking ({len(pcm) / 2 / SAMPLE_RATE:.1f}s)")
            await self.to_say.put(pcm)
            # Wait for the reply: first agent audio after our speech end.
            waited = time.perf_counter()
            while "first_audio_at" not in self.current:
                if self.session_closed:
                    return self._stop("session_closed_during_turn")
                if time.perf_counter() - waited > turn_timeout_s:
                    self.current["timeout"] = True
                    return self._stop(f"timeout_waiting_for_reply_{index}")
                await asyncio.sleep(0.05)
            self._finish_turn()
        # Let the last reply play out before leaving.
        await asyncio.sleep(8.0)
        return self._stop("completed")

    def _finish_turn(self, t: dict | None = None) -> None:
        t = t if t is not None else (self.current or {})
        if "speech_end_at" in t and "first_audio_at" in t:
            t["client_ttfa_ms"] = round((t["first_audio_at"] - t["speech_end_at"]) * 1000)
        if "speech_end_at" in t and "processing_at" in t:
            t["client_speech_end_to_processing_ms"] = round((t["processing_at"] - t["speech_end_at"]) * 1000)
        if "speech_end_at" in t and "reply_audio_at" in t:
            t["client_ttmr_ms"] = round((t["reply_audio_at"] - t["speech_end_at"]) * 1000)
        if t is self.current:
            self.log(f"turn {t.get('turn')}: client TTFA {t.get('client_ttfa_ms')} ms")

    def _stop(self, reason: str) -> str:
        self.stop_reason = reason
        for t in self.turns:
            self._finish_turn(t)  # client TTMR lands after the first-audio wait
        return reason

    async def close(self) -> None:
        for task in [self._mic_task, *self._ear_tasks]:
            if task is not None:
                task.cancel()
        try:
            await self.room.disconnect()
        except Exception:  # noqa: BLE001
            pass


# -------------------------------------------------------------------- main


async def pilot(args: argparse.Namespace) -> dict[str, Any]:
    from dotenv import dotenv_values
    from sqlalchemy import text

    from src.infrastructure.database import StandaloneSessionFactory

    env = dotenv_values(args.env_file)
    secret = env.get("JWT_SECRET") or "development-only-secret-change-me"
    algorithm = env.get("JWT_ALGORITHM") or "HS256"
    lines: list[str] = []

    def log(message: str) -> None:
        stamp = datetime.now(UTC).strftime("%H:%M:%S")
        lines.append(f"{stamp} {message}")
        print(f"{stamp} {message}", flush=True)

    api = Api(args.api, mint_token(secret, algorithm, args.user, args.email))
    record: dict[str, Any] = {
        "config": args.config, "started_at": datetime.now(UTC).isoformat(), "turns_requested": args.turns,
        "budget_calls": args.budget_calls, "pilot": True,
        "note": "pilot: one session of ~8 turns per configuration; no P95/P99 claims",
    }
    try:
        record["container_flags"] = container_flags(args.compose_file)
        session = await api.post(f"{API_INTERVIEWS}/sessions", {
            "resumeId": args.resume, "jobId": args.job, "mode": "voice", "experienceType": "voice_interview",
            "locale": "vi-VN", "durationMinutes": args.duration_minutes,
        })
        sid = str(session.get("id") or session.get("sessionId") or session.get("session", {}).get("id"))
        record["session_id"] = sid
        log(f"session {sid} created ({args.config})")
        plan = await api.post(f"{API_INTERVIEWS}/sessions/{sid}/plan")
        record["plan"] = {"planId": plan.get("planId"), "policyVersion": plan.get("policyVersion"), "targets": len(plan.get("targets", []))}
        frozen = await api.post(f"{API_INTERVIEWS}/sessions/{sid}/questions/select")
        record["frozen"] = {"turns": len(frozen.get("turns", [])), "fallbackQuestionCount": frozen.get("fallbackQuestionCount")}
        log(f"plan {record['plan']} frozen {record['frozen']}")
        token = await api.post(f"{API_VOICE}/livekit-token", {"sessionId": sid, "roomName": f"interview-{sid}"})
        record["agent_name"] = token.get("agentName")

        async def call_counter() -> int:
            async with StandaloneSessionFactory() as db:
                return int(await db.scalar(text("SELECT count(*) FROM ai_call_logs WHERE session_id = :sid"), {"sid": sid}) or 0)

        answers = [_load_wav(Path(args.audio_dir) / f"answer_{i:02d}.wav") for i in range(1, args.turns + 1)]
        candidate = Candidate(answers, turns=args.turns, budget_calls=args.budget_calls, call_counter=call_counter, log=log)
        await candidate.connect(token["serverUrl"], token["participantToken"])
        log("joined the room; waiting for the agent")
        record["stop_reason"] = await candidate.run()
        record["client_turns"] = candidate.turns
        await candidate.close()
        try:
            await api.post(f"{API_INTERVIEWS}/sessions/{sid}/close")
        except SystemExit as exc:
            log(f"close: {exc}")
        await asyncio.sleep(3.0)  # let the agent flush its last events

        metrics = _metrics_module()
        _, events, calls = await metrics.load(sid)
        record["server_metrics"] = metrics.summarize(events, calls)
        record["calls_total"] = len(calls)
        record["models"] = sorted({f"{c.get('model')}" for c in calls if c.get("model")})
        record["purposes"] = sorted({str(c.get("purpose")) for c in calls})
        started_events = [e for e in events if e.get("event") == "voice_session_started"]
        record["voice_session_started"] = (started_events[0].get("payload") if started_events else None)
        ttfa = [t["client_ttfa_ms"] for t in candidate.turns if t.get("client_ttfa_ms") is not None]
        record["client_ttfa_ms"] = {"n": len(ttfa), "values": ttfa, "p50": metrics.percentile([float(v) for v in ttfa], 50), "max": max(ttfa) if ttfa else None}
        ttmr = [t["client_ttmr_ms"] for t in candidate.turns if t.get("client_ttmr_ms") is not None]
        record["client_ttmr_ms"] = {"n": len(ttmr), "values": ttmr, "p50": metrics.percentile([float(v) for v in ttmr], 50), "max": max(ttmr) if ttmr else None}
        record["client_speech_end_to_processing_ms"] = [t.get("client_speech_end_to_processing_ms") for t in candidate.turns]
        record["late_transcripts"] = sum(1 for t in candidate.turns if t.get("late_transcript"))
    finally:
        record["log"] = lines
        record["finished_at"] = datetime.now(UTC).isoformat()
        await api.close()
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", required=True,
                        choices=("baseline", "unified", "anticipation", "endpointing", "backchannel"))
    parser.add_argument("--api", default="http://localhost:5000")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--user", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--resume", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--audio-dir", required=True)
    parser.add_argument("--turns", type=int, default=8)
    parser.add_argument("--budget-calls", type=int, default=40, help="stop the session once ai_call_logs exceeds this")
    parser.add_argument("--duration-minutes", type=int, default=15)
    parser.add_argument("--compose-file", action="append", default=["docker-compose.yml"])
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    record = asyncio.run(pilot(args))
    Path(args.out).write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: record.get(k) for k in ("config", "session_id", "stop_reason", "calls_total", "client_ttfa_ms")}, ensure_ascii=False, default=str))
    return 0 if record.get("stop_reason") == "completed" else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
