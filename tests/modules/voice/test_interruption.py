import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.modules.interviews.adapters.voice.interruption import (
    InterruptionSnapshot,
    capture_interruption,
    classify_interruption,
    delivered_text_from_handle,
    hold_acknowledgement,
    is_greeting_backchannel,
    is_probable_playout_echo,
    is_repeat_request,
    is_resume_request,
    listening_acknowledgement,
    remaining_after_interruption,
    resolve_interruption,
)

SCENARIOS = json.loads(
    (Path(__file__).parents[2] / "fixtures" / "voice" / "interruption_scenarios.json").read_text(
        encoding="utf-8"
    )
)
QUESTION = "Bạn hãy giới thiệu ngắn về bản thân và kinh nghiệm gần nhất nhé?"


@pytest.mark.parametrize(
    "text", ["Dạ", "vâng.", "ừm", "OK", "được", "vậy ạ", "Hello", "Alo", "xin chào"]
)
def test_vietnamese_backchannels_do_not_become_candidate_answers(text: str) -> None:
    assert classify_interruption(text, locale="vi-VN") == "backchannel"


@pytest.mark.parametrize("text", ["Khoan", "khoan đã", "chờ chút", "từ từ"])
def test_vietnamese_hold_requests_pause_without_answering(text: str) -> None:
    assert classify_interruption(text, locale="vi-VN") == "hold"


@pytest.mark.parametrize(
    "text",
    [
        "Không, ý của tôi là dùng Redis.",
        "Tôi muốn giải thích lại phần database.",
        "Khoan, câu hỏi này có nghĩa là gì?",
    ],
)
def test_substantive_overlap_is_forwarded_to_interview_core(text: str) -> None:
    assert classify_interruption(text, locale="vi-VN") == "content"


@pytest.mark.parametrize("text", ["mhm", "okay", "right", "uh huh"])
def test_english_backchannels(text: str) -> None:
    assert classify_interruption(text, locale="en-US") == "backchannel"


@dataclass
class _Message:
    text_content: str


class _Handle:
    id = "speech-1"
    interrupted = True
    chat_items = [_Message("Câu hỏi đã được phát")]


def test_capture_interruption_uses_playout_truncated_text() -> None:
    snapshot = capture_interruption(_Handle(), "Câu hỏi đã được phát đầy đủ chưa?")

    assert snapshot.occurred is True
    assert snapshot.speech_id == "speech-1"
    assert snapshot.delivered_text == "Câu hỏi đã được phát"
    assert snapshot.telemetry()["expected_text"] == "Câu hỏi đã được phát đầy đủ chưa?"


def test_non_interrupted_handle_produces_empty_snapshot() -> None:
    handle = _Handle()
    handle.interrupted = False
    assert capture_interruption(handle, "Question").occurred is False


def test_remaining_text_uses_exact_prefix() -> None:
    assert (
        remaining_after_interruption(
            "Bạn hãy mô tả kiến trúc hệ thống của mình.",
            "Bạn hãy mô tả kiến trúc",
        )
        == "hệ thống của mình."
    )


def test_remaining_text_tolerates_whitespace_normalization() -> None:
    assert remaining_after_interruption("Một câu hỏi\n\nngắn cho bạn", "Một câu hỏi ngắn") == "cho bạn"


def test_remaining_text_does_not_guess_when_transcript_diverges() -> None:
    assert remaining_after_interruption("Câu hỏi đầy đủ", "Nội dung khác") == ""


def test_remaining_text_replays_all_when_nothing_was_delivered() -> None:
    assert remaining_after_interruption("Câu hỏi đầy đủ", "") == "Câu hỏi đầy đủ"


def test_acknowledgements_follow_locale() -> None:
    assert listening_acknowledgement("vi-VN") == "Vâng."
    assert listening_acknowledgement("en-US") == "Sure."
    assert hold_acknowledgement("vi-VN").startswith("Vâng")


def test_delivered_text_handles_missing_sdk_shape() -> None:
    assert delivered_text_from_handle(object()) == ""


def test_detects_greeting_that_must_not_advance_interview() -> None:
    assert is_greeting_backchannel("Hello.", locale="vi-VN")
    assert is_greeting_backchannel("Alo", locale="vi-VN")
    assert not is_greeting_backchannel("Dạ, em có kinh nghiệm.", locale="vi-VN")


@pytest.mark.parametrize(
    "text",
    [
        "Bị đứt đoạn á.",
        "Em chưa nghe rõ.",
        "Bạn nói lại giúp mình nhé.",
        "Âm thanh bị ngắt.",
        "mình nghe chưa rõ bạn có thể nói lại được không?",
        "Mình nghe chưa rõ.",
        "Em nghe không rõ ạ",
        "Bạn hỏi gì cơ?",
        "Gì cơ ạ?",
        "Bạn nói chậm lại được không",
        "Em chưa nghe được câu hỏi",
        "Câu hỏi là gì vậy ạ",
    ],
)
def test_detects_vietnamese_repeat_requests(text: str) -> None:
    assert is_repeat_request(text, locale="vi-VN")


@pytest.mark.parametrize(
    "text",
    [
        "Em có ba năm kinh nghiệm backend.",
        # Answer content that happens to use "hỏi lại" / "lặp lại".
        "Em hỏi lại PM để chốt yêu cầu rồi mới code.",
        "Khi request lỗi em cho retry, lặp lại tối đa ba lần với backoff rồi mới báo lỗi cho người dùng.",
        # Accent-folded, "họ" would read as the request word "hộ".
        "Em hỏi lại họ để chốt yêu cầu.",
        "Em nghe lại recording để tìm lỗi.",
        "Kết nối DB bị ngắt thì em retry.",
        "Trong hệ thống không có gì có thể thay thế Redis.",
    ],
)
def test_does_not_treat_candidate_answer_as_repeat_request(text: str) -> None:
    assert not is_repeat_request(text, locale="vi-VN")


@pytest.mark.parametrize("case", SCENARIOS, ids=lambda case: case["id"])
def test_real_world_interruption_decision_matrix(case: dict[str, str]) -> None:
    resolution = resolve_interruption(
        case["transcript"],
        InterruptionSnapshot(
            occurred=True,
            speech_id="speech-1",
            expected_text=QUESTION,
            delivered_text=case["delivered"],
        ),
        locale="vi-VN",
    )
    assert resolution.action == case["action"]
    assert resolution.reason == case["reason"]
    assert resolution.should_forward is (case["action"] == "forward")
    if case["action"] == "resume":
        assert resolution.resume_text


def test_playout_echo_requires_a_non_trivial_contiguous_fragment() -> None:
    assert is_probable_playout_echo("hãy giới thiệu ngắn về bản thân", QUESTION)
    assert not is_probable_playout_echo("kinh nghiệm", QUESTION)
    assert not is_probable_playout_echo("Redis cache aside", QUESTION)


@pytest.mark.parametrize(
    ("text", "locale"),
    [("Tiếp tục đi", "vi-VN"), ("Tôi sẵn sàng rồi", "vi-VN"), ("go ahead", "en-US")],
)
def test_resume_request_after_hold(text: str, locale: str) -> None:
    assert is_resume_request(text, locale=locale)


def test_normal_non_overlapping_turn_is_always_forwarded() -> None:
    resolution = resolve_interruption("Có.", InterruptionSnapshot(), locale="vi-VN")
    assert resolution.action == "forward"
    assert resolution.reason == "normal_turn"
