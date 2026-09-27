# OpenAI voice lab (experimental)

This branch adds an isolated, disabled-by-default way to compare three audio paths. It does not select questions, evaluate answers, or write to the question bank. All requests require the existing bearer login and ownership of an existing interview session.

Set these in the backend `.env` (do not commit the actual key):

```dotenv
OPENAI_API_KEY=your-server-side-api-key
VOICE_LAB_ENABLED=true
VOICE_LAB_REALTIME_MODEL=gpt-realtime-2.1
VOICE_LAB_TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe
VOICE_LAB_TTS_MODEL=gpt-4o-mini-tts
VOICE_LAB_VOICE=marin
```

Restart the API. Use `/docs` with your normal login bearer token and a session ID that belongs to your account:

| Route | Input | Result | What it measures |
| --- | --- | --- | --- |
| `POST /ai/voice-lab/transcribe` | multipart `sessionId`, `audio` (webm, wav, mp3, mp4; <= 10 MiB) | `{text, elapsedMs}` | Recorded audio to final text; **not** streaming STT |
| `POST /ai/voice-lab/speak` | JSON `{sessionId, text}` | MP3, `X-Voice-Lab-Elapsed-Ms` | Text to completed MP3; **not** first-audio latency |
| `POST /ai/voice-lab/realtime-token` | JSON `{sessionId}` | Short-lived client secret | Browser WebRTC speech-to-speech setup |

For the Realtime path, use the returned `value` as the browser's short-lived bearer credential to establish WebRTC with `POST https://api.openai.com/v1/realtime/calls` and the browser's SDP offer. Keep the regular `OPENAI_API_KEY` on the server. The Realtime model is not yet wired to the question selector or rubric; do not use this path to score a real interview. The two bounded REST audio routes allow a quick API smoke test, while Realtime requires a browser peer connection. No live provider call runs in automated tests.

Official OpenAI references: https://developers.openai.com/api/docs/guides/voice-webrtc and https://developers.openai.com/api/docs/guides/audio.
