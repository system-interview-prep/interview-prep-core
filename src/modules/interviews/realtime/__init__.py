"""Realtime data plane: the audio lifecycle around one spoken turn.

INTERVIA_INTERVIEW_AGENT_V2_SPEC §8 and §10. LiveKit → VAD → Qwen STT →
endpointing → the interview runtime → validated response events → VieNeu TTS →
playback. Audio frames never enter the LangGraph checkpoint, and chat bypasses
this package entirely.

``runtime.py`` is the LiveKit worker and the docker-compose entry point;
``endpointing.py`` decides when the candidate has finished speaking;
``interruption.py`` handles barge-in; ``stt/`` and ``tts/`` hold the provider
clients. Nothing here decides what to ask -- that belongs to ``agents/``.
"""
