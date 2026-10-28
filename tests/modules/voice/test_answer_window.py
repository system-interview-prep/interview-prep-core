"""One spoken answer = one Interview Core turn, however many STT fragments it has."""

import pytest

from src.modules.interviews.adapters.voice.answer_window import (
    SHORT_ANSWER_WORDS,
    AnswerWindow,
    silence_reprompt,
    turn_taking_tip,
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


@pytest.mark.parametrize("locale", ["vi-VN", "en-US"])
def test_voice_guidance_is_hands_free(locale: str) -> None:
    guidance = f"{turn_taking_tip(locale)} {silence_reprompt(locale)}".lower()
    assert "button" not in guidance
    assert "bấm" not in guidance
    assert "press" not in guidance


def test_processing_filler_rotates_between_answers() -> None:
    from src.modules.interviews.adapters.voice.answer_window import processing_filler, processing_fillers

    lines = [processing_filler("vi-VN", i) for i in range(len(processing_fillers("vi-VN")))]
    assert len(set(lines)) == len(lines) >= 3
    assert processing_filler("vi-VN") == "Ừm, chờ mình một chút nhé."
    assert processing_filler("en-US", 1) != processing_filler("en-US", 0)


def test_filler_varies_by_session_and_the_reply_does_not_repeat_its_interjection() -> None:
    from src.modules.interviews.adapters.voice.answer_window import (
        PROCESSING_FILLERS_VI,
        pick_processing_filler,
        without_leading_interjection,
    )

    picks = {pick_processing_filler("vi-VN", f"session-{i}:1") for i in range(40)}
    assert len(picks) >= 4  # not always the same first line
    for i in range(20):
        last = pick_processing_filler("vi-VN", f"s:{i}")
        assert pick_processing_filler("vi-VN", f"s:{i}", avoid=last) != last
    assert sum(not line.startswith(("Ừm", "Ok", "À")) for line in PROCESSING_FILLERS_VI) >= 4
    assert without_leading_interjection("Ừm, mình hiểu ý bạn.\n\nCâu hỏi?") == "Mình hiểu ý bạn.\n\nCâu hỏi?"
    assert without_leading_interjection("Ok.") == "Ok."
    assert without_leading_interjection("Giờ mình qua Docker.") == "Giờ mình qua Docker."


def test_room_lines_follow_the_voice_dialect_and_acknowledgements_are_cached(monkeypatch) -> None:
    from src.modules.interviews.adapters.voice.answer_window import localize_particles, scripted_reply_lines
    from src.modules.interviews.core import interviewer_script as script

    assert set(script.ACK_FALLBACK_VI) <= set(scripted_reply_lines("vi-VN"))
    monkeypatch.setenv("INTERVIEWER_VOICE_DIALECT", "nam")
    assert localize_particles("Ừm, chờ mình một chút nhé.") == "Ừm, chờ mình một chút nha."
