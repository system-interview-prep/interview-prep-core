"""The shared interviewer script: one wording for chat, voice and video.

Checks the shared phrase style and verifies that voice-only recovery phrases
remain in the Voice adapter.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from src.modules.interviews.core.dialogue import phrases as script

BANNED = ("hãy", "vui lòng", "ghi nhận")
SENSORY = re.compile(r"\b(nghe|đọc|gõ)\b", re.IGNORECASE)
MAX_SENTENCE_WORDS = 25


def _vietnamese_lines() -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    for name in dir(script):
        if not name.endswith("_VI"):
            continue
        value = getattr(script, name)
        if isinstance(value, str):
            lines.append((name, value))
        elif isinstance(value, tuple):
            lines += [(name, item) for item in value]
        elif isinstance(value, dict):
            for item in value.values():
                lines += [(name, line) for line in (item if isinstance(item, tuple) else (item,))]
    return lines


LINES = _vietnamese_lines()


def test_script_has_every_situation() -> None:
    names = {name for name, _ in LINES}
    for expected in (
        "OPENING_GREETING_VI", "WARMUP_QUESTION_VI", "ACK_FALLBACK_VI", "SKIP_ACK_VI", "GIVE_UP_ACK_VI",
        "QUESTION_RESPONSES_VI", "ABORT_CONFIRMATION_VI", "CLOSE_COMPLETED_VI", "USER_ENDED_VI",
    ):
        assert expected in names


@pytest.mark.parametrize(("name", "line"), LINES)
def test_line_follows_the_house_style(name: str, line: str) -> None:
    lowered = line.casefold()
    for word in BANNED:
        assert word not in lowered, (name, line)
    assert "INTERVIA" not in line, (name, line)
    for sentence in re.split(r"(?<=[.?!;:])\s+", line):
        assert len(sentence.split()) <= MAX_SENTENCE_WORDS, (name, sentence)
    assert lowered.count("nhé") <= 1, (name, line)
    # "mình" is always the interviewer, never the candidate.
    assert "về mình" not in lowered and "gì mình biết" not in lowered, (name, line)
    assert not SENSORY.search(line), (name, line)


def test_acknowledgements_do_not_praise_or_judge_the_candidate() -> None:
    from src.modules.interviews.core.dialogue.policy import is_evaluative, is_judgmental

    ack_pools = {"ACK_FALLBACK_VI", "INTRO_ACK_VI", "NON_ANSWER_ACK_VI", "GIVE_UP_ACK_VI", "SKIP_ACK_VI"}
    for name, line in LINES:
        if name not in ack_pools:
            continue
        assert not is_evaluative(line) and not is_judgmental(line), (name, line)


def test_voice_only_feedback_is_kept_in_the_voice_adapter() -> None:
    from src.modules.interviews.adapters.voice.conversation import answer_window, interruption, voice_phrases
    from src.modules.interviews.core.dialogue import policy as interviewer_dialogue

    assert answer_window.PROCESSING_FILLERS_VI == voice_phrases.PROCESSING_FILLERS_VI
    assert answer_window.ANSWER_PAUSE_NUDGES_VI == voice_phrases.ANSWER_PAUSE_NUDGES_VI
    assert answer_window.not_heard_lines("vi-VN") == voice_phrases.NOT_HEARD_VI
    assert answer_window.silence_reprompt("vi-VN") == voice_phrases.SILENCE_REPROMPT_VI
    assert answer_window.turn_taking_tip("vi-VN") == voice_phrases.TURN_TAKING_TIP_VI
    assert answer_window.unclear_transcript_text("vi-VN") == voice_phrases.UNCLEAR_TRANSCRIPT_VI
    assert answer_window.core_error_text("vi-VN") == voice_phrases.CORE_ERROR_VI
    assert interruption.hold_acknowledgement("vi-VN") == voice_phrases.HOLD_ACK_VI
    assert interviewer_dialogue.SKIP_ACK_VI is script.SKIP_ACK_VI


def test_voice_agent_packages_load_without_livekit_runtime_imports() -> None:
    """Pure voice conversation helpers remain importable as package modules."""
    project_root = Path(__file__).resolve().parents[3]
    code = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from src.modules.interviews.adapters.voice.conversation import answer_window, interruption\n"
        "assert answer_window.silence_reprompt('vi-VN')\n"
        "assert interruption.hold_acknowledgement('vi-VN')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(project_root)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr


def test_every_room_answers_an_aside_by_repeating_the_question() -> None:
    from src.modules.interviews.core.interview_engine import candidate_question_response

    for spoken in (True, False):
        reply = candidate_question_response(
            "ROLE_QA", current_question="Bạn làm phần nào?", is_vi=True, duration_minutes=25, spoken=spoken
        )
        assert reply == (
            "Câu này mình xin để dành đến phần hỏi đáp cuối buổi.\n\nMình hỏi lại nhé: Bạn làm phần nào?"
        )
