"""The shared interviewer script: one wording for chat, voice and video.

Checks the house style on every scripted line (docs: interview/
INTERVIEWER_SCRIPT_UNIFIED_2026-10-20.md) and that the voice agent reads the
same file as Interview Core.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from src.modules.interviews.core import interviewer_script as script

# Lines only the spoken rooms need; they may talk about hearing and speaking.
VOICE_ONLY = {
    "PROCESSING_FILLERS_VI",
    "NOT_HEARD_VI",
    "UNCLEAR_TRANSCRIPT_VI",
    "CORE_ERROR_VI",
    "SILENCE_REPROMPT_VI",
    "TURN_TAKING_TIP_VI",
    "BACKCHANNEL_CONTINUE_VI",
    "HOLD_ACK_VI",
    "LISTENING_ACK_VI",
}
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
        "PROCESSING_FILLERS_VI", "NOT_HEARD_VI", "SILENCE_REPROMPT_VI",
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
    if name not in VOICE_ONLY:
        assert not SENSORY.search(line), (name, line)


def test_acknowledgements_do_not_praise_or_judge_the_candidate() -> None:
    from src.modules.interviews.core.interviewer_voice import is_evaluative, is_judgmental

    ack_pools = {"ACK_FALLBACK_VI", "INTRO_ACK_VI", "NON_ANSWER_ACK_VI", "GIVE_UP_ACK_VI", "SKIP_ACK_VI"}
    for name, line in LINES:
        if name not in ack_pools:
            continue
        assert not is_evaluative(line) and not is_judgmental(line), (name, line)


def test_core_and_voice_agent_use_the_same_lines() -> None:
    from src.modules.interviews.adapters.voice import answer_window, interruption
    from src.modules.interviews.core import interviewer_voice

    assert answer_window.PROCESSING_FILLERS_VI == script.PROCESSING_FILLERS_VI
    assert answer_window.not_heard_lines("vi-VN") == script.NOT_HEARD_VI
    assert answer_window.silence_reprompt("vi-VN") == script.SILENCE_REPROMPT_VI
    assert answer_window.turn_taking_tip("vi-VN") == script.TURN_TAKING_TIP_VI
    assert answer_window.unclear_transcript_text("vi-VN") == script.UNCLEAR_TRANSCRIPT_VI
    assert answer_window.core_error_text("vi-VN") == script.CORE_ERROR_VI
    assert interruption.hold_acknowledgement("vi-VN") == script.HOLD_ACK_VI
    # Interview Core keeps the old import names.
    assert interviewer_voice.SKIP_ACK_VI is script.SKIP_ACK_VI


def test_voice_agent_loads_the_script_without_the_src_package(tmp_path: Path) -> None:
    """The LiveKit worker runs adapters/voice as plain scripts (no ``src`` on the path)."""
    voice_dir = Path(__file__).resolve().parents[3] / "src" / "modules" / "interviews" / "adapters" / "voice"
    code = (
        "import sys\n"
        "sys.path = [p for p in sys.path if 'interview-prep-core' not in p or p.endswith('voice')]\n"
        "import answer_window, interruption\n"
        "assert 'src' not in sys.modules\n"
        "print(answer_window.silence_reprompt('vi-VN'))\n"
        "print(interruption.hold_acknowledgement('vi-VN'))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=voice_dir, capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert script.SILENCE_REPROMPT_VI in result.stdout
    assert script.HOLD_ACK_VI in result.stdout


def test_every_room_answers_an_aside_by_repeating_the_question() -> None:
    from src.modules.interviews.core.interview_engine import candidate_question_response

    for spoken in (True, False):
        reply = candidate_question_response(
            "ROLE_QA", current_question="Bạn làm phần nào?", is_vi=True, duration_minutes=25, spoken=spoken
        )
        assert reply == (
            "Câu này mình xin để dành đến phần hỏi đáp cuối buổi.\n\nMình hỏi lại nhé: Bạn làm phần nào?"
        )
