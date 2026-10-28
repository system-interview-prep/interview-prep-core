# Interview Voice

## Runtime flow

The authenticated voice interview uses the existing interview session, locked P2 plan, and chat runtime:

```text
Browser microphone -> LiveKit STT -> Interview Core chat runtime -> selected TTS provider -> Browser audio
```

LiveKit does not attach a free-form LLM. It sends each final transcript to Interview Core and speaks the response returned by the current runtime. Interview Core continues to use its configured model where its existing decision logic requires it.

## Configuration

Set these values in the project-root `.env` file:

```env
VOICE_LAB_ENABLED=true
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
LIVEKIT_AGENT_NAME=intervia-voice
LIVEKIT_AGENT_AUTOSTART=true
LIVEKIT_AGENT_IDLE_PROCESSES=1
LIVEKIT_AGENT_MAX_RETRY=32
LIVEKIT_AGENT_RUN_MODE=dev
LIVEKIT_STT_PROVIDER=assemblyai
LIVEKIT_STT_MODEL=assemblyai/universal-3-5-pro
LIVEKIT_STT_LANGUAGE=session
VOICE_LAB_STT_STEERING=prompt
VOICE_LAB_STT_MIN_TURN_SILENCE_MS=128
VOICE_LAB_STT_MAX_TURN_SILENCE_MS=900
VOICE_LAB_STT_VAD_THRESHOLD=0.30
VOICE_LAB_NOISE_CANCELLATION=bvc
VOICE_LAB_VAD_MIN_SPEECH_DURATION=0.20
VOICE_LAB_VAD_MIN_SILENCE_DURATION=0.65
VOICE_LAB_VAD_ACTIVATION_THRESHOLD=0.50
VOICE_LAB_VAD_DEACTIVATION_THRESHOLD=0.35
VOICE_LAB_ENDPOINT_MAX_DELAY=1.8
VOICE_LAB_INTERRUPTION_MODE=adaptive
VOICE_LAB_INTERRUPTION_MIN_DURATION=0.7
VOICE_LAB_INTERRUPTION_MIN_WORDS=2
VOICE_LAB_FALSE_INTERRUPTION_TIMEOUT=2.0
VOICE_LAB_BACKCHANNEL_START_BOUNDARY=0.5
VOICE_LAB_BACKCHANNEL_END_BOUNDARY=2.2
VOICE_LAB_AEC_WARMUP_DURATION=1.0
VOICE_LAB_MIN_CONSECUTIVE_SPEECH_DELAY=0.18
VOICE_LAB_TTS_PROVIDER=elevenlabs  # elevenlabs | vieneu | cartesia
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...
ELEVENLABS_VI_VOICE_ID=...
ELEVENLABS_EN_VOICE_ID=...
ELEVENLABS_STABILITY=0.45
ELEVENLABS_SIMILARITY_BOOST=0.75
ELEVENLABS_STYLE=0
ELEVENLABS_SPEED=0.98
ELEVENLABS_SPEAKER_BOOST=true
ELEVENLABS_AUTO_MODE=true
ELEVENLABS_TEXT_NORMALIZATION=auto
VIENEU_TTS_BASE_URL=http://host.docker.internal:8000
VIENEU_TTS_API_KEY=...
# The existing VIENEU_API_KEY name is also accepted as a fallback.
VIENEU_TTS_VOICE=
VIENEU_TTS_TIMEOUT_SECONDS=180
VOICE_RUNTIME_API_BASE_URL=http://127.0.0.1:5000
VOICE_AGENT_DNS_PRIMARY=1.1.1.1
VOICE_AGENT_DNS_SECONDARY=8.8.8.8
```

When the LiveKit worker runs in a separate container or service, set `VOICE_RUNTIME_API_BASE_URL` to an address it can use, such as `http://backend:5000`. The interview voice path does not require an OpenAI LLM in the LiveKit worker. `OPENAI_API_KEY` is still used by the separate `/transcribe`, `/speak`, and Realtime configuration endpoints.

Install the optional LiveKit dependencies with `python -m pip install -e ".[voice-realtime]"`. The API starts the worker when `LIVEKIT_AGENT_AUTOSTART=true`; to run it separately, use `python src/modules/interviews/adapters/voice/livekit_agent.py dev`.

## Production resilience

The Compose voice worker uses two configurable upstream DNS resolvers and three
resolver attempts. In private networks, replace the public defaults with the
organization's redundant resolvers. `LIVEKIT_AGENT_MAX_RETRY` controls the
worker's in-process registration retry budget; after it is exhausted, Docker's
`restart: unless-stopped` starts a clean worker.

Provider failures are published to the browser on `intervia.health` without raw
exception text or credentials. The UI tells the candidate to pause while a
recoverable STT/TTS outage retries, then clears the warning after the next
successful provider metric. LiveKit signaling reconnects are shown separately.
Monitor the structured log events `Voice provider degraded`, `Voice provider
recovered`, `registered worker`, and `Voice STT metrics`. Alert on a degraded
event without a matching recovery, repeated worker registration, or sessions
with user speech but no final STT metric.

## Vietnamese WER/CER benchmark

The versioned corpus protocol lives in
`../DOC_AND_PLAN/data/eval/stt/vi_v1/README.md`. Once consented audio and a
human-verified `manifest.jsonl` exist, export provider hypotheses and run:

```powershell
python scripts/evaluate_vi_stt.py --manifest <manifest.jsonl> --predictions <predictions.jsonl>
```

The scorer reports micro WER/CER, S/D/I counts, exact-match rate, prediction
coverage, and `dialect × environment` slices. Missing hypotheses count as full
deletions, preventing provider/network failures from being hidden by scoring
only successful requests.

## Start a voice interview

1. Create an interview session with `mode: "voice"`.
2. Prepare and lock its P2 question plan.
3. Request `POST /ai/voice-lab/livekit-token` with the user's bearer token and `{ "sessionId": "...", "roomName": "..." }`.
4. Connect the browser to the returned `serverUrl` with `participantToken` and publish the microphone.

The API verifies session ownership, voice mode, and locked plan. It passes the worker a short-lived credential scoped to that session. The worker starts the existing chat runtime, speaks the approved opening message, and forwards each final transcript as a `VOICE` turn. The LiveKit API secret is never returned to the browser. The session-scoped runtime credential is attached as signed agent-dispatch metadata, not returned as a separate API field.

## Notes

- The browser must have microphone permission and connect to the configured LiveKit server.
- Choose `LIVEKIT_STT_PROVIDER=assemblyai` with
  `LIVEKIT_STT_MODEL=assemblyai/universal-3-5-pro` for streaming AssemblyAI, or
  `LIVEKIT_STT_PROVIDER=qwen` with `QWEN_STT_BASE_URL`, `QWEN_STT_MODEL`, and
  `QWEN_STT_CHUNK_SIZE_SECONDS` for Qwen's official chunk-streaming API. The
  adapter finalizes a Qwen utterance after `QWEN_STT_ENDPOINT_SILENCE_SECONDS`
  of trailing audio below `QWEN_STT_SILENCE_RMS_THRESHOLD`; defaults are 0.8s
  and 0.008. LiveKit VAD still decides when to submit the completed turn. For
  Windows/Docker Desktop, point the base URL at the EC2 public DNS/IP on port
  8001 (the AWS security group should restrict that port to your administrator
  CIDR). Qwen emits interim transcripts as audio chunks are decoded, then a final
  transcript when Silero VAD ends the utterance. The model may revise its recent
  words as more audio arrives; shorter chunks can increase GPU load and do not
  guarantee lower end-to-end latency.
- With Qwen, `VOICE_LLM_SPECULATIVE_PREVIEW=1` (default) sends stable interim
  transcript snapshots to a read-only Core preview. It is rate-limited to at
  most three model evaluations per answer. The final turn reuses a preview only
  when its signed token matches the exact final transcript and still-active
  question; otherwise the normal Core evaluation runs. Preview text is never
  committed or spoken by itself.
- AssemblyAI Universal-3.5 Pro handles Vietnamese/English code-switching. STT turn
  silence, Silero sensitivity, endpoint delay, and barge-in thresholds can be tuned
  independently with the values above.
- `LIVEKIT_STT_LANGUAGE=session` locks recognition to the interview locale for
  the whole call (`vi` for `vi-VN`, `en` for `en-US`). A single provider prompt
  preserves English technical vocabulary; AssemblyAI does not receive the
  mutually exclusive `prompt` and `keyterms_prompt` options together.
- Normal interviewer speech is interruptible. The final closing remains
  non-interruptible so the session-close event is delivered only after playout.
- Adaptive interruption ignores short listener backchannels. If it degrades to
  VAD, the transport resumes the unheard suffix for backchannels, treats short
  hold requests without advancing the plan, and acknowledges genuine barge-ins
  while Interview Core prepares its response.
- Browser AEC/noise suppression/AGC is followed by LiveKit BVC on the worker.
  Set `VOICE_LAB_NOISE_CANCELLATION=off` only for self-hosted deployments where
  the LiveKit Cloud filter is unavailable. Literal STT noise tokens and probable
  TTS echo fragments are discarded before Interview Core can score them.
- Set `VOICE_LAB_TTS_PROVIDER=elevenlabs` or `vieneu`. The VieNeu adapter calls
  `/v1/audio/speech` with `stream_format=sse`, strips the streamed WAV header,
  and forwards PCM frames to LiveKit as they arrive. With Docker Desktop and an
  SSH tunnel to the EC2 TTS server, use `VIENEU_TTS_BASE_URL=http://host.docker.internal:8000`.
- ElevenLabs handles TTS with the interview locale passed explicitly (`vi` or
  `en`). Configure a native-accent voice per locale; the generic voice remains
  only a compatibility fallback. Auto mode removes chunk-schedule buffering
  because every interview utterance is supplied as complete text.
- The existing OpenAI Realtime experiment is separate and does not control this interview flow.
