from types import SimpleNamespace

from src.modules.interviews.adapters.voice.health import provider_error_payload, provider_recovered_payload


def test_provider_error_payload_exposes_safe_recovery_state_only():
    event = SimpleNamespace(
        error=SimpleNamespace(type="stt_error", recoverable=True, error=RuntimeError("secret-url")),
        source=SimpleNamespace(provider="assemblyai"),
    )
    payload = provider_error_payload(event, session_id="session-1")
    assert payload == {
        "type": "voice.transport.degraded",
        "sessionId": "session-1",
        "component": "stt",
        "provider": "assemblyai",
        "recoverable": True,
    }
    assert "secret-url" not in str(payload)


def test_provider_recovered_payload_is_stable():
    assert provider_recovered_payload(session_id="session-1", component="stt") == {
        "type": "voice.transport.recovered",
        "sessionId": "session-1",
        "component": "stt",
    }
