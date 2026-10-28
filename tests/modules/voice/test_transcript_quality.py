from src.modules.interviews.adapters.voice.transcript_quality import assess_transcript


def test_rejects_low_confidence_transcript() -> None:
    result = assess_transcript(
        "I'm a big dream. em nghe ngày nay nghe một tiếng là đấy.",
        confidence=0.31,
        min_confidence=0.45,
    )

    assert result.accepted is False
    assert result.reason == "low_confidence"


def test_accepts_when_provider_does_not_supply_confidence() -> None:
    result = assess_transcript(
        "Em đã xây dựng REST API bằng FastAPI và PostgreSQL.",
        confidence=None,
        min_confidence=0.45,
    )

    assert result.accepted is True


def test_accepts_vietnamese_with_english_technical_terms() -> None:
    result = assess_transcript(
        "Em dùng Docker, Kubernetes và CI CD để triển khai hệ thống.",
        confidence=0.82,
        min_confidence=0.45,
    )

    assert result.accepted is True


def test_rejects_repeated_noise_tokens() -> None:
    result = assess_transcript("ừ ừ ừ ừ ừ", confidence=0.9, min_confidence=0.45)

    assert result.accepted is False
    assert result.reason == "repeated_token"


def test_rejects_common_english_hallucination_in_vietnamese_session() -> None:
    result = assess_transcript(
        "Thank you for watching.",
        confidence=None,
        min_confidence=0.45,
        locale="vi-VN",
    )

    assert result.accepted is False
    assert result.reason == "locale_mismatch"


def test_accepts_vietnamese_code_switch_without_provider_confidence() -> None:
    result = assess_transcript(
        "Em dùng Docker, Kubernetes và CI CD để deploy.",
        confidence=None,
        min_confidence=0.45,
        locale="vi-VN",
    )

    assert result.accepted is True


def test_accepts_standalone_technical_terms_in_vietnamese_session() -> None:
    result = assess_transcript(
        "Docker Kubernetes PostgreSQL",
        confidence=None,
        min_confidence=0.45,
        locale="vi-VN",
    )

    assert result.accepted is True
