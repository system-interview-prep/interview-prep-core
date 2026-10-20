import pytest

from src.modules.interviews.adapters.voice.language import (
    INTERVIEW_KEYTERMS,
    assemblyai_session_options,
    assemblyai_stream_options,
    normalize_transcript_for_locale,
    speakable_text,
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


def test_vietnamese_call_greeting_is_not_persisted_as_hello() -> None:
    assert normalize_transcript_for_locale("Hello,", locale="vi-VN") == "Alô."
    assert normalize_transcript_for_locale("Hello, hello.", locale="vi-VN") == "Alô, alô."
    assert normalize_transcript_for_locale("Hello, bạn nghe rõ không?", locale="vi-VN") == (
        "Alô, bạn nghe rõ không?"
    )
    assert normalize_transcript_for_locale("Yeah, em nghe rõ à?", locale="vi-VN") == "Dạ, em nghe rõ à?"


def test_transcript_normalization_does_not_rewrite_real_answers() -> None:
    # Only the opening filler changes; the answer is kept word for word.
    answer = "Yeah, em dùng PostgreSQL cho transaction chính."
    assert normalize_transcript_for_locale(answer, locale="vi-VN") == (
        "Dạ, em dùng PostgreSQL cho transaction chính."
    )
    english = "Yes, I used Redis as a cache."
    assert normalize_transcript_for_locale(english, locale="vi-VN") == english
    assert normalize_transcript_for_locale("Hello everyone", locale="vi-VN") == "Hello everyone"
    assert normalize_transcript_for_locale("Yeah.", locale="en-US") == "Yeah."
    assert normalize_transcript_for_locale("Hello, can you hear me?", locale="en-US") == (
        "Hello, can you hear me?"
    )


def test_vietnamese_stt_configuration_is_fixed_and_non_conflicting() -> None:
    options = assemblyai_session_options(
        "vi",
        agent_context="Bạn giới thiệu ngắn về kinh nghiệm gần nhất nhé?",
    )
    assert options["language_detection"] is False
    assert "keyterms_prompt" not in options
    assert "do not render them as yeah" in str(options["prompt"])
    assert options["agent_context"] == "Bạn giới thiệu ngắn về kinh nghiệm gần nhất nhé?"


def test_keyterms_fit_the_provider_limit_and_cover_accented_terms() -> None:
    assert len(INTERVIEW_KEYTERMS) <= 100
    assert len(set(INTERVIEW_KEYTERMS)) == len(INTERVIEW_KEYTERMS)
    assert all(len(term) <= 50 for term in INTERVIEW_KEYTERMS)
    for term in ("alô", "backend", "Docker", "Redis", "React", "frontend", "query"):
        assert term in INTERVIEW_KEYTERMS


def test_prompt_steering_stays_available_by_environment() -> None:
    options = assemblyai_stream_options("vi", env={"VOICE_LAB_STT_STEERING": "prompt"})
    assert "keyterms_prompt" not in options
    assert "do not render them as yeah" in str(options["prompt"])
    assert "prompt" in assemblyai_stream_options("vi", env={"VOICE_LAB_STT_STEERING": "bogus"})


def test_interviewer_speech_reads_notation_as_words() -> None:
    assert speakable_text("Bên em chạy CI/CD trên GitHub Actions.", locale="vi-VN") == (
        "Bên em chạy CI CD trên GitHub Actions."
    )
    assert speakable_text("Bạn dùng C# và .NET hay Node.Js?", locale="vi-VN") == (
        "Bạn dùng C sharp và dot NET hay Node JS?"
    )
    assert speakable_text("REST/GraphQL", locale="vi-VN") == "REST hoặc GraphQL"
    assert speakable_text("REST/GraphQL", locale="en-US") == "REST or GraphQL"
    # Numbers and links are left alone.
    assert speakable_text("1/2 dữ liệu, https://a.com/x", locale="vi-VN") == "1/2 dữ liệu, https://a.com/x"


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
    assert result.startswith("Mình hỏi theo cách khác nhé:")


def test_interviewer_speech_reads_numbers_units_and_layout_aloud() -> None:
    from src.modules.interviews.adapters.voice.language import number_to_vietnamese

    assert [number_to_vietnamese(n) for n in (5, 14, 15, 21, 24, 25, 105, 1005, 2025)] == [
        "năm", "mười bốn", "mười lăm", "hai mươi mốt", "hai mươi tư", "hai mươi lăm",
        "một trăm linh năm", "một nghìn không trăm linh năm", "hai nghìn không trăm hai mươi lăm",
    ]
    spoken = speakable_text(
        "Ừ, bạn đặt TTL 5 phút.\n\nBuổi 25′ còn 30″, độ tin cậy 0,7 và 80% thời gian.", locale="vi-VN"
    )
    assert spoken == (
        "Ừ, bạn đặt TTL năm phút. Buổi hai mươi lăm phút còn ba mươi giây, "
        "độ tin cậy không phẩy bảy và tám mươi phần trăm thời gian."
    )
    # Numbers that are part of a name are read as the name.
    assert speakable_text("S3, HTTP2, v2.5 hay Python 3.12", locale="vi-VN") == "S3, HTTP2, v2.5 hay Python 3.12"
    assert speakable_text("Còn 25′.\n\nTiếp", locale="en-US") == "Còn 25 minutes. Tiếp"
