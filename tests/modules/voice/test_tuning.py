from src.modules.interviews.adapters.voice.tuning import voice_tuning


def test_noise_resistant_defaults_keep_real_barge_in_responsive() -> None:
    tuning = voice_tuning({})
    assert tuning.noise_cancellation == "bvc"
    assert tuning.vad_activation_threshold == 0.5
    assert tuning.vad_deactivation_threshold < tuning.vad_activation_threshold
    assert tuning.vad_min_speech_duration == 0.2
    assert tuning.interruption_min_duration == 0.7
    assert tuning.false_interruption_timeout == 2.0


def test_invalid_settings_fall_back_or_are_clamped_safely() -> None:
    tuning = voice_tuning(
        {
            "VOICE_LAB_NOISE_CANCELLATION": "unknown",
            "VOICE_LAB_VAD_ACTIVATION_THRESHOLD": "broken",
            "VOICE_LAB_VAD_DEACTIVATION_THRESHOLD": "0.99",
            "VOICE_LAB_VAD_MIN_SPEECH_DURATION": "-10",
            "VOICE_LAB_FALSE_INTERRUPTION_TIMEOUT": "99",
        }
    )
    assert tuning.noise_cancellation == "bvc"
    assert tuning.vad_activation_threshold == 0.5
    assert tuning.vad_deactivation_threshold == 0.45
    assert tuning.vad_min_speech_duration == 0.1
    assert tuning.false_interruption_timeout == 5.0


def test_noise_cancellation_can_be_disabled_for_non_cloud_deployments() -> None:
    assert voice_tuning({"VOICE_LAB_NOISE_CANCELLATION": "off"}).noise_cancellation == "off"
