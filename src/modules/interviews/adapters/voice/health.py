"""Small, provider-agnostic helpers for voice transport health events."""

from __future__ import annotations

from typing import Any


def provider_error_payload(
    event: object,
    *,
    session_id: str,
) -> dict[str, Any]:
    """Convert a LiveKit provider error into a stable browser contract.

    Provider exception strings may contain URLs or credentials, so they are
    intentionally kept in server logs and never sent to the browser.
    """
    error = getattr(event, "error", None)
    source = getattr(event, "source", None)
    error_type = str(getattr(error, "type", "provider_error"))
    component = error_type.removesuffix("_error")
    if component not in {"stt", "tts", "llm", "realtime_model", "interruption_detection"}:
        component = "voice"
    payload: dict[str, Any] = {
        "type": "voice.transport.degraded",
        "sessionId": session_id,
        "component": component,
        "provider": str(getattr(source, "provider", "unknown")),
        "recoverable": bool(getattr(error, "recoverable", False)),
    }
    if _is_rate_limited(getattr(error, "error", None)):
        # LiveKit Inference refuses with 429 at the plan's connection cap
        # (Build: 5 STT) or once the monthly credit is spent (Build: $2.50,
        # shared by STT and TTS). Reconnecting cannot help; the room says so.
        payload["reason"] = "rate_limited"
    return payload


def _is_rate_limited(exc: object) -> bool:
    """True for HTTP 429 anywhere in the exception chain (status or handshake text)."""
    seen = 0
    while exc is not None and seen < 5:
        if getattr(exc, "status_code", None) == 429 or "429" in str(exc):
            return True
        exc = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
        seen += 1
    return False


def provider_recovered_payload(*, session_id: str, component: str) -> dict[str, Any]:
    return {
        "type": "voice.transport.recovered",
        "sessionId": session_id,
        "component": component,
    }
