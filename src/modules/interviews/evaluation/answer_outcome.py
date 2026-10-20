"""Separate real answers from skip requests, "không biết" and empty fragments.

An asked question that the candidate skipped is scored 0 and counts toward the
average. It is not "uncovered": that status is for questions the system never
asked, and treating skips the same way would let a candidate skip every hard
question and be graded only on the easy ones. The grader used to score these
turns itself and gave 0-0.5 for "honesty", inconsistently between turns.

A voice turn that holds only an empty fragment ("dạ", "là câu này em.") is a
different case: it comes from STT cutting the answer or the agent failing, not
from the candidate. It is NOT_GRADABLE: shown on the report, left out of the
score and counted as uncovered.
"""

from __future__ import annotations

import re
import unicodedata

from src.modules.interviews.core.interview_engine import (
    classify_candidate_intent,
    is_skip_request,
    is_yes_no_question,
)

ANSWERED = "ANSWERED"
SKIPPED = "SKIPPED"
NOT_GRADABLE = "NOT_GRADABLE"

# Message kinds.
ANSWER = "ANSWER"
NON_ANSWER = "NON_ANSWER"
FRAGMENT = "FRAGMENT"

_NON_ANSWER_INTENTS = frozenset({"SKIP_REQUEST", "GIVE_UP"})

# Words a pure skip or "I don't know" is made of. A message that still has
# other words after removing these may carry an attempt ("promise thì .catch
# bắt, còn lại em chưa rõ, qua câu khác") and goes to the grader.
_FILLER_WORDS = frozenset(
    """
    dạ vâng ạ à ừ ừm ờ ơ ơi thì là mà nhé nha nhá hả vậy thôi luôn rồi nữa đâu ra
    cái câu này đó kia phần chủ đề hỏi
    em mình tôi anh chị bạn
    cũng không ko k chưa biết rõ lắm thật sự hiểu nhớ học chịu bó tay thua
    có thể được cho xin phép giúp với đổi qua bỏ chuyển sang khác tiếp
    kinh nghiệm làm về
    alo xin lỗi sorry ok okay
    i don't dont do not know no idea have skip next question pass this sorry
    can we move on to another one
    """.split()
)
_MAX_CONTENT_WORDS = 1

# Phrases that state "I can't answer this", as opposed to a bare "dạ" or "ok".
_STATED_NON_ANSWER = re.compile(
    r"\b(không|ko|k|chưa)\s+(biết|rõ|nhớ|học|hiểu)\b|\bchịu\b|\bbó\s+tay\b|\bbỏ\s+qua\b"
    r"|\bchưa\s+có\s+kinh\s+nghiệm\b|\bpass\b|\bskip\b|\bdo\s*n'?t\s+know\b|\bno\s+idea\b"
)


def _content_words(text: str) -> list[str]:
    words = re.findall(r"[\w']+", unicodedata.normalize("NFC", text).lower())
    return [word for word in words if word not in _FILLER_WORDS]


def _states_non_answer(text: str) -> bool:
    spoken = unicodedata.normalize("NFC", text).lower()
    return is_skip_request(spoken) or bool(_STATED_NON_ANSWER.search(spoken))


def classify_message(message: str, question_prompt: str = "") -> str:
    """ANSWER, NON_ANSWER (skip / "không biết") or FRAGMENT (no content at all)."""
    if not message.strip():
        return FRAGMENT
    content = len(_content_words(message))
    if classify_candidate_intent(message, question_prompt) in _NON_ANSWER_INTENTS:
        if content > _MAX_CONTENT_WORDS:
            return ANSWER
        return NON_ANSWER if _states_non_answer(message) else FRAGMENT
    # "Chưa" / "Không" answer a yes/no question even with no other words.
    if content == 0 and not is_yes_no_question(question_prompt):
        return NON_ANSWER if _states_non_answer(message) else FRAGMENT
    return ANSWER


def is_non_answer(message: str, question_prompt: str = "") -> bool:
    """True for a skip request or "không biết" that carries no attempt at all."""
    return classify_message(message, question_prompt) == NON_ANSWER


def split_answer_messages(
    messages: list[str], question_prompt: str = ""
) -> tuple[list[str], list[str]]:
    """Return (answers, everything else) for the candidate messages of one turn."""
    answers: list[str] = []
    rest: list[str] = []
    for message in messages:
        (answers if classify_message(message, question_prompt) == ANSWER else rest).append(message)
    return answers, rest


def turn_outcome(
    messages: list[str], question_prompt: str = "", *, is_voice: bool = False
) -> tuple[str, list[str]]:
    """Outcome of one asked turn and the messages the grader should see."""
    kinds = [classify_message(message, question_prompt) for message in messages]
    answers = [message for message, kind in zip(messages, kinds, strict=True) if kind == ANSWER]
    if answers:
        return ANSWERED, answers
    if NON_ANSWER in kinds or not is_voice:
        return SKIPPED, []
    # Voice turn with only fragments, or no transcript at all.
    return NOT_GRADABLE, []
