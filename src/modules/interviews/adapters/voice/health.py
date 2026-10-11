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
    return {
        "type": "voice.transport.degraded",
        "sessionId": session_id,
        "component": component,
        "provider": str(getattr(source, "provider", "unknown")),
        "recoverable": bool(getattr(error, "recoverable", False)),
    }


def provider_recovered_payload(*, session_id: str, component: str) -> dict[str, Any]:
    return {
        "type": "voice.transport.recovered",
        "sessionId": session_id,
        "component": component,
    }
