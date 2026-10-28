import pytest

from src.modules.interviews.adapters.voice.stt_provider import stt_provider_settings


def test_assemblyai_provider_defaults_to_universal_35_pro():
    settings = stt_provider_settings({})
    assert settings.provider == "assemblyai"
    assert settings.model == "assemblyai/universal-3-5-pro"
    assert settings.base_url is None


def test_qwen_provider_uses_official_chunk_streaming_endpoint():
    settings = stt_provider_settings(
        {
            "LIVEKIT_STT_PROVIDER": "qwen",
            "QWEN_STT_BASE_URL": "http://qwen.example:8001/",
            "QWEN_STT_MODEL": "Qwen/Qwen3-ASR-1.7B",
            "QWEN_STT_CHUNK_SIZE_SECONDS": "0.8",
        }
    )
    assert settings.provider == "qwen"
    assert settings.model == "Qwen/Qwen3-ASR-1.7B"
    assert settings.base_url == "http://qwen.example:8001"
    assert settings.chunk_size_seconds == 0.8
    assert settings.endpoint_silence_seconds == 0.8
    assert settings.silence_rms_threshold == 0.008


def test_qwen_provider_accepts_configurable_silence_endpoint():
    settings = stt_provider_settings(
        {
            "LIVEKIT_STT_PROVIDER": "qwen",
            "QWEN_STT_BASE_URL": "http://qwen.example:8001",
            "QWEN_STT_ENDPOINT_SILENCE_SECONDS": "1.2",
            "QWEN_STT_SILENCE_RMS_THRESHOLD": "0.01",
        }
    )
    assert settings.endpoint_silence_seconds == 1.2
    assert settings.silence_rms_threshold == 0.01


@pytest.mark.parametrize(
    "env, message",
    [
        ({"LIVEKIT_STT_PROVIDER": "invalid"}, "must be 'assemblyai' or 'qwen'"),
        ({"LIVEKIT_STT_PROVIDER": "qwen"}, "QWEN_STT_BASE_URL is required"),
        (
            {
                "LIVEKIT_STT_PROVIDER": "qwen",
                "QWEN_STT_BASE_URL": "http://qwen:8001/v1",
            },
            "without /v1",
        ),
        (
            {
                "LIVEKIT_STT_PROVIDER": "qwen",
                "QWEN_STT_BASE_URL": "http://qwen:8001",
                "QWEN_STT_CHUNK_SIZE_SECONDS": "0.1",
            },
            "between 0.2 and 5 seconds",
        ),
        (
            {
                "LIVEKIT_STT_PROVIDER": "qwen",
                "QWEN_STT_BASE_URL": "http://qwen:8001",
                "QWEN_STT_ENDPOINT_SILENCE_SECONDS": "0.1",
            },
            "QWEN_STT_ENDPOINT_SILENCE_SECONDS must be between 0.3 and 3 seconds",
        ),
        (
            {
                "LIVEKIT_STT_PROVIDER": "qwen",
                "QWEN_STT_BASE_URL": "http://qwen:8001",
                "QWEN_STT_SILENCE_RMS_THRESHOLD": "0",
            },
            "QWEN_STT_SILENCE_RMS_THRESHOLD must be between 0.001 and 0.05",
        ),
        (
            {"LIVEKIT_STT_MODEL": "Qwen/Qwen3-ASR-1.7B"},
            "must be an AssemblyAI model",
        ),
    ],
)
def test_invalid_stt_provider_settings_fail_fast(env, message):
    with pytest.raises(ValueError, match=message):
        stt_provider_settings(env)
