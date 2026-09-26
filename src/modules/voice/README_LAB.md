# INTERVIA Voice Lab

Experimental voice-only workspace. It is intentionally isolated from Interview Core, DB writes, and Question Bank.

## Goal

Run a native realtime speech pipeline with LiveKit WebRTC:

1. Deepgram Nova-3 multilingual streaming STT
2. Streaming LLM response
3. Streaming ElevenLabs TTS

for Vietnamese IT interviews with Vietnamese/English code-switching.

## Run

Install Streamlit locally:

```bash
pip install streamlit
```

Configure the environment using `src/modules/voice/.env.example`, then run:

```bash
streamlit run src/modules/voice/streamlit_lab.py
```

## LiveKit streaming pipeline

The LiveKit agent provides a full duplex streaming path:

```text
LiveKit microphone -> Deepgram Nova-3 multi STT -> OpenAI LLM -> ElevenLabs TTS -> LiveKit audio
```

Install and start the agent from the repository root:

```bash
python -m pip install -e ".[voice-realtime]"
python src/modules/voice/livekit_agent.py dev
```

Configure these values in `src/modules/voice/.env`:

```env
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
LIVEKIT_AGENT_NAME=intervia-voice
OPENAI_API_KEY=...
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...
VOICE_LAB_LLM_MODEL=gpt-5-mini
VOICE_LAB_STT_LANGUAGE=vi
```

The client must join the LiveKit room and publish its microphone track. The
agent then streams interim/final STT transcripts, LLM output, and synthesized
audio through the same room. The upload-based cascaded action is disabled in
Streamlit so recording cannot accidentally trigger a full MP3 request.

The agent defaults to `deepgram/nova-3:vi` because Vietnamese is the primary
interview language. Set `VOICE_LAB_STT_LANGUAGE=multi` to enable automatic
language detection for Vietnamese/English code-switching; short phrases can be
less reliable in automatic detection mode.

For a local multi-turn latency test, start the agent first, then open Streamlit,
select the `LiveKit Realtime Speech` tab, and press `Start LiveKit streaming`.
Each click creates a new room so explicit agent dispatch is applied. Press
`Connect & start test`, wait for `Agent joined`, then repeat the cycle
`You → AI → You → AI` for as many turns as needed. The embedded client reports
each user transcript and the corresponding `turn N response speech` timestamp.
It uses the browser LiveKit client and a short-lived token generated from the
local environment file.

The agent uses VAD endpointing with roughly 0.8–1.2 seconds of silence, so a
turn is committed automatically after you stop speaking. Microphone echo
cancellation and noise suppression are enabled in the browser client.

For the main authenticated frontend, request a short-lived room token from
`POST /ai/voice-lab/livekit-token` with `{ "sessionId": "...", "roomName": "..." }`.
The endpoint includes explicit dispatch for `LIVEKIT_AGENT_NAME` and never
returns the LiveKit API secret to the browser.

## Current scope

- Browser microphone capture through Streamlit
- OpenAI transcription test
- OpenAI text LLM interviewer test
- ElevenLabs TTS test
- Sequential latency measurements
- OpenAI Realtime session/client-secret configuration validation
- No Interview Core calls
- No database writes
- No Question Bank calls

## Next lab step

Add a browser/WebRTC test page for native OpenAI Realtime and a LiveKit transport option while preserving the same prompt and benchmark cases.

Measure:

- end-of-turn -> first audio
- technical-term correctness
- Vietnamese/English code-switch behavior
- clarification quality
- interruption behavior
- cost per interview
