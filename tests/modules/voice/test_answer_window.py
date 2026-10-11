"""One spoken answer = one Interview Core turn, however many STT fragments it has."""

import pytest

from src.modules.interviews.adapters.voice.answer_window import (
    SHORT_ANSWER_WORDS,
    AnswerWindow,
    is_done_command,
)


def test_fragments_of_one_answer_are_submitted_together() -> None:
    window = AnswerWindow(commit_silence=2.5, short_commit_silence=5.0)
    # What AssemblyAI produced for one 19-second answer in the live run.
    window.add("Em là Khoa, backend developer.", message_id="m1", duration=2.0)
    window.add("Em từng làm API đồng bộ đơn hàng.", message_id="m2", duration=3.0)
    window.add("Xử lý webhook trùng bằng idempotency.", message_id="m3", duration=4.0)

    answer = window.take()

    assert answer is not None
    assert answer.fragment_count == 3
    assert answer.text.startswith("Em là Khoa") and answer.text.endswith("idempotency.")
    # Retries of the same answer reuse one id.
    assert answer.client_message_id == "livekit:m1"
    assert answer.duration_seconds == 9.0
    assert window.is_empty and window.take() is None


def test_short_buffer_waits_longer_before_submitting() -> None:
    window = AnswerWindow(commit_silence=2.5, short_commit_silence=5.0)
    window.add("Chưa ạ.", message_id="m1")
    assert window.commit_delay() == 5.0
    window.add(" ".join(["từ"] * SHORT_ANSWER_WORDS), message_id="m2")
    assert window.commit_delay() == 2.5


def test_blank_fragments_are_ignored() -> None:
    window = AnswerWindow()
    window.add("   ", message_id="m1")
    assert window.is_empty and window.first_message_id is None


@pytest.mark.parametrize(
    "text",
    ["Xong.", "Em trả lời xong rồi ạ.", "Dạ, em xong rồi.", "Hết rồi ạ", "Vậy thôi ạ."],
)
def test_short_done_phrases_hand_the_floor_back(text: str) -> None:
    assert is_done_command(text)


@pytest.mark.parametrize(
    "text",
    [
        "Deploy xong thì em chạy smoke test và theo dõi log.",
        "Khi xong bước một em chuyển sang bước hai",
        "Em làm xong phần API trong hai tuần với Node.js và PostgreSQL",
        "",
    ],
)
def test_xong_inside_a_sentence_is_content_not_a_command(text: str) -> None:
    assert not is_done_command(text)


def test_english_done_phrases() -> None:
    assert is_done_command("That's all.", locale="en-US")
    assert not is_done_command("Xong.", locale="en-US")
