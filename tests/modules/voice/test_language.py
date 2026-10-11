import pytest

from src.modules.interviews.adapters.voice.language import (
    assemblyai_session_options,
    normalize_transcript_for_locale,
)
from src.modules.interviews.core.interview_engine import (
    SAFE_FALLBACK_PROBE_VI,
    InterviewCoreEngine,
)
from src.modules.interviews.core.language_policy import response_matches_locale


class _EnglishDriftLLM:
    async def generate_text(self, system_prompt: str, user_content: str) -> str:
        return "How would you handle retries and circuit breakers?"


def test_vietnamese_short_acknowledgement_is_not_persisted_as_english() -> None:
    assert normalize_transcript_for_locale("Yeah.", locale="vi-VN") == "Dạ."
    assert normalize_transcript_for_locale("Yeah, yeah.", locale="vi-VN") == "Dạ, dạ."


def test_transcript_normalization_does_not_rewrite_real_answers() -> None:
    answer = "Yeah, em dùng PostgreSQL cho transaction chính."
    assert normalize_transcript_for_locale(answer, locale="vi-VN") == answer
    assert normalize_transcript_for_locale("Yeah.", locale="en-US") == "Yeah."


def test_vietnamese_stt_configuration_is_fixed_and_non_conflicting() -> None:
    options = assemblyai_session_options(
        "vi",
        agent_context="Bạn giới thiệu ngắn về kinh nghiệm gần nhất nhé?",
    )
    assert options["language_detection"] is False
    assert "keyterms_prompt" not in options
    assert "dạ" in str(options["prompt"])
    assert "do not render them as yeah" in str(options["prompt"])
    assert options["agent_context"] == "Bạn giới thiệu ngắn về kinh nghiệm gần nhất nhé?"


def test_generated_response_language_guard_allows_technical_code_switching() -> None:
    assert response_matches_locale(
        "Bạn xử lý retry và circuit breaker như thế nào?", is_vi=True
    )
    assert not response_matches_locale(
        "How would you handle retries and circuit breakers?", is_vi=True
    )
    assert not response_matches_locale("Explain Redis replication.", is_vi=True)
    assert response_matches_locale("Redis hay PostgreSQL phù hợp hơn?", is_vi=True)
    assert response_matches_locale("How would you handle retries?", is_vi=False)
    assert not response_matches_locale("Bạn xử lý retry thế nào?", is_vi=False)


@pytest.mark.asyncio
async def test_english_probe_is_replaced_in_vietnamese_session() -> None:
    engine = InterviewCoreEngine(llm_client=_EnglishDriftLLM())
    result = await engine._generate_probe_question(
        "Em dùng exponential backoff.",
        {"main_prompt": "Bạn xử lý lỗi gọi API như thế nào?"},
        is_vi=True,
    )
    assert result == SAFE_FALLBACK_PROBE_VI


@pytest.mark.asyncio
async def test_english_clarification_is_replaced_in_vietnamese_session() -> None:
    engine = InterviewCoreEngine(llm_client=_EnglishDriftLLM())
    result = await engine._generate_clarify_text(
        {"main_prompt": "Bạn xử lý lỗi gọi API như thế nào?"},
        "Bạn giải thích lại giúp mình?",
        is_vi=True,
    )
    assert result.startswith("Ý của câu hỏi")
