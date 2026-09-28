# Interview Voice

## Runtime flow

The authenticated voice interview uses the existing interview session, locked P2 plan, and chat runtime:

```text
Browser microphone -> LiveKit STT -> Interview Core chat runtime -> ElevenLabs TTS -> Browser audio
```

LiveKit does not attach a free-form LLM. It sends each final transcript to Interview Core and speaks the response returned by the current runtime. Interview Core continues to use its configured model where its existing decision logic requires it.

## Configuration

Set these values in the API environment:

```env
VOICE_LAB_ENABLED=true
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
LIVEKIT_AGENT_NAME=intervia-voice
LIVEKIT_AGENT_AUTOSTART=true
LIVEKIT_AGENT_RUN_MODE=dev
LIVEKIT_STT_MODEL=assemblyai/universal-3-5-pro
LIVEKIT_STT_LANGUAGE=auto
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...
VOICE_RUNTIME_API_BASE_URL=http://127.0.0.1:5000
```

When the LiveKit worker runs in a separate container or service, set `VOICE_RUNTIME_API_BASE_URL` to an address it can use, such as `http://backend:5000`. The interview voice path does not require an OpenAI LLM in the LiveKit worker. `OPENAI_API_KEY` is still used by the separate `/transcribe`, `/speak`, and Realtime configuration endpoints.

Install the optional LiveKit dependencies with `python -m pip install -e ".[voice-realtime]"`. The API starts the worker when `LIVEKIT_AGENT_AUTOSTART=true`; to run it separately, use `python src/modules/voice/livekit_agent.py dev`.

## Start a voice interview

1. Create an interview session with `mode: "voice"`.
2. Prepare and lock its P2 question plan.
3. Request `POST /ai/voice-lab/livekit-token` with the user's bearer token and `{ "sessionId": "...", "roomName": "..." }`.
4. Connect the browser to the returned `serverUrl` with `participantToken` and publish the microphone.

The API verifies session ownership, voice mode, and locked plan. It passes the worker a short-lived credential scoped to that session. The worker starts the existing chat runtime, speaks the approved opening message, and forwards each final transcript as a `VOICE` turn. The LiveKit API secret is never returned to the browser. The session-scoped runtime credential is attached as signed agent-dispatch metadata, not returned as a separate API field.

## Notes

- The browser must have microphone permission and connect to the configured LiveKit server.
- AssemblyAI or Deepgram handles STT; ElevenLabs handles TTS.
- The existing OpenAI Realtime experiment is separate and does not control this interview flow.
