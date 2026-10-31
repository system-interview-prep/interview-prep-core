"""One spoken answer = one Interview Core turn, however many STT fragments it has."""

from pathlib import Path

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


def test_room_never_speaks_a_waiting_filler() -> None:
    """No "Ừm, chờ mình một chút nhé" while Core works (removed 31/10)."""
    from src.modules.interviews.adapters.voice import answer_window
    from src.modules.interviews.core import interviewer_script as script

    assert not hasattr(script, "PROCESSING_FILLERS_VI")
    assert not hasattr(answer_window, "processing_filler")
    agent = Path(answer_window.__file__).with_name("livekit_agent.py").read_text(encoding="utf-8")
    assert 'kind="filler"' not in agent


def test_room_lines_follow_the_voice_dialect_and_acknowledgements_are_cached(monkeypatch) -> None:
    from src.modules.interviews.adapters.voice.answer_window import localize_particles, scripted_reply_lines
    from src.modules.interviews.core import interviewer_script as script

    assert set(script.ACK_FALLBACK_VI) <= set(scripted_reply_lines("vi-VN"))
    monkeypatch.setenv("INTERVIEWER_VOICE_DIALECT", "nam")
    assert localize_particles("Ừm, chờ mình một chút nhé.") == "Ừm, chờ mình một chút nha."
