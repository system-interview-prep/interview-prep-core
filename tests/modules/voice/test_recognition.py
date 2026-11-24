from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.modules.interviews.adapters.voice.stt import recognition
from src.modules.interviews.adapters.voice.stt.stt_provider import stt_provider_settings
from src.modules.interviews.adapters.voice.config.tuning import voice_tuning


def test_assemblyai_factory_uses_shared_language_and_context(monkeypatch):
    constructor, options = Mock(), Mock(return_value={"prompt": "context-prompt"})
    monkeypatch.setattr(recognition.inference, "STT", constructor)
    monkeypatch.setattr(recognition, "assemblyai_stream_options", options)
    recognition.create_stt(stt_provider_settings({}), "vi", "Backend Developer")
    options.assert_called_once_with("vi", agent_context="Backend Developer")
    assert constructor.call_args.kwargs["extra_kwargs"] == {"prompt": "context-prompt"}


def test_qwen_factory_passes_all_settings_and_does_not_claim_context_support(monkeypatch):
    constructor, sink = Mock(), Mock()
    monkeypatch.setattr(recognition, "QwenStreamingSTT", constructor)
    settings = stt_provider_settings(
        {
            "LIVEKIT_STT_PROVIDER": "qwen",
            "QWEN_STT_BASE_URL": "http://qwen",
            "QWEN_STT_CHUNK_SIZE_SECONDS": "0.4",
        }
    )
    recognition.create_stt(settings, "en", "Context is not supported by the demo", telemetry=sink)
    kwargs = constructor.call_args.kwargs
    assert kwargs["language"] == "en"
    assert kwargs["chunk_size_seconds"] == 0.4
    assert kwargs["endpoint_silence_seconds"] == settings.endpoint_silence_seconds
    assert kwargs["telemetry"] is sink
    assert "context" not in kwargs


@pytest.mark.parametrize("provider, detection, delay", [("qwen", "vad", 0.8), ("assemblyai", "stt", 0)])
def test_shared_vad_and_endpointing_do_not_depend_on_test_mode(monkeypatch, provider, detection, delay):
    vad = Mock()
    monkeypatch.setattr(recognition.inference, "VAD", vad)
    tuning = voice_tuning({})
    options = recognition.recognition_options(SimpleNamespace(provider=provider), tuning)
    assert options["turn_handling"]["turn_detection"] == detection
    assert options["turn_handling"]["endpointing"]["min_delay"] == delay
    assert vad.call_args.kwargs["activation_threshold"] == tuning.vad_activation_threshold
    assert options["llm"] is None


def test_test_room_binds_the_expected_admin_and_allows_explicit_final_drain():
    options = recognition.recognition_room_options(
        voice_tuning({"VOICE_LAB_NOISE_CANCELLATION": "off"}), participant_identity="admin-1"
    )
    assert options.participant_identity == "admin-1"
    assert options.close_on_disconnect is False
    assert options.audio_input.auto_gain_control is True
