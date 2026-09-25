# INTERVIA Voice Lab

Experimental voice-only workspace. It is intentionally isolated from Interview Core, DB writes, and Question Bank.

## Goal

Compare:

1. Native realtime speech model
2. Cascaded STT -> text LLM -> ElevenLabs TTS

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
