import json
from pathlib import Path

import pytest

from src.modules.interviews.adapters.voice.interruption import (
    classify_interruption,
    is_repeat_request,
    remaining_after_interruption,
)
from src.modules.interviews.adapters.voice.language import (
    assemblyai_session_options,
    normalize_transcript_for_locale,
)
from src.modules.interviews.adapters.voice.provider_config import tts_provider_config

GOLDEN = json.loads(
    (Path(__file__).parents[2] / "fixtures" / "voice" / "vi_pipeline_golden.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("case", GOLDEN["stt_normalization"], ids=lambda case: case["input"])
def test_vietnamese_stt_golden(case: dict[str, str]) -> None:
    assert normalize_transcript_for_locale(case["input"], locale="vi-VN") == case["expected"]


@pytest.mark.parametrize("case", GOLDEN["overlap"], ids=lambda case: case["input"])
def test_vietnamese_overlap_golden(case: dict[str, str]) -> None:
    assert classify_interruption(case["input"], locale="vi-VN") == case["intent"]


@pytest.mark.parametrize("text", GOLDEN["replay_requests"])
def test_vietnamese_replay_request_golden(text: str) -> None:
    assert is_repeat_request(text, locale="vi-VN")


def test_zero_playout_interruption_replays_the_complete_question() -> None:
    question = "Bạn giới thiệu ngắn về bản thân và kinh nghiệm gần nhất nhé?"
    assert remaining_after_interruption(question, "") == question


def test_provider_configuration_matches_vietnamese_golden() -> None:
    expected = GOLDEN["provider"]
    stt = assemblyai_session_options(expected["stt_language"])
    tts = tts_provider_config(expected["locale"], {})

    assert stt["language_detection"] is expected["stt_language_detection"]
    assert tts.language == expected["tts_language"]
    assert tts.model == expected["tts_model"]
    assert tts.auto_mode is expected["tts_auto_mode"]
    assert tts.apply_text_normalization == expected["tts_text_normalization"]


def test_invalid_tts_normalization_env_falls_back_safely() -> None:
    assert (
        tts_provider_config("vi-VN", {"ELEVENLABS_TEXT_NORMALIZATION": "invalid"})
        .apply_text_normalization
        == "auto"
    )


def test_tts_can_force_session_locale_for_monolingual_deployments() -> None:
    config = tts_provider_config("vi-VN", {"ELEVENLABS_LANGUAGE_MODE": "locale"})

    assert config.language == "vi"
