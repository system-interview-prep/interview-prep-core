"""Rule checks on one interview turn: why the engine acted, and what looks wrong.

Tầng 1 of DOC_AND_PLAN/plan/interview/LLM_MONITORING_DESIGN_2026-10-25.md: no
model call, run on every turn. A flag does not change the interview; it tags
the turn for the LLM judge and the review queue in Langfuse, and is stored on
the ``turn_spoken`` event for the admin pages. Post-session checks (grades
against the in-session verdicts) are in ``session_flags``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from src.modules.interviews.core.interviewer_voice import looks_like_injection

# S5 of the design: the interviewer talks about itself as a system.
# ("mình là người phỏng vấn AI của Intervia" in the opening is allowed.)
_SYSTEM_LEAK_RE = re.compile(
    r"mô hình ngôn ngữ|language model|(?:tôi|mình) là (?:một )?(?:mô hình|chatbot|trí tuệ nhân tạo)"
    r"|được lập trình|system prompt|hướng dẫn hệ thống|lời nhắc hệ thống|\bopenai\b|\bchatgpt\b|\bgpt-\d"
    r"|\bas an ai\b|\bi am an ai\b",
    re.IGNORECASE,
)
# Function words that say nothing about the topic of an answer.
_STOPWORDS = frozenset(
    """
    và hoặc là thì mà của cho với trong ngoài trên dưới khi nếu vì do để này đó kia các những một hai ba
    có không chưa đã đang sẽ rồi cũng vẫn còn được bị bạn mình em anh chị tôi họ nó ta chúng thế nào
    gì sao vậy như rất khá lắm nhiều ít hơn nhất thêm nữa lại ra vào lên xuống đi đến từ theo về qua
    việc cách phần cái điều người lúc thì ở ạ nhé nha à ừ ừm ok vâng dạ thì là the a an of to and or in
    on for is are be it this that with as at by from we you i
    """.split()
)

FLAG_LABELS = (
    "guard_reject",
    "llm_error",
    "injection",
    "giveup_but_long",
    "answer_off_question",
    "probe_off_target",
    "system_leak",
)


def _words(text: str) -> set[str]:
    value = unicodedata.normalize("NFC", str(text or "")).casefold()
    return {w for w in re.findall(r"[\w+#./-]+", value) if len(w) >= 2 and w not in _STOPWORDS}


def decision_rule(action: str, meta: dict[str, Any]) -> str:
    """The engine branch behind the action, in one code (the "why" of the turn)."""
    action = str(action or "").upper()
    if meta.get("skipped_turn"):
        return "skip_request"
    if meta.get("clarify_used") or action == "CLARIFY":
        return "clarify_request"
    if action == "CONFIRM_ABORT" or meta.get("candidate_abort_intent"):
        return "abort_request"
    if meta.get("is_qna_followup"):
        return "candidate_question"
    if meta.get("hard_answer_timeout"):
        return "answer_timeout"
    if action in {"TERMINATE", "WRAP_UP"}:
        return f"end_{str(meta.get('exit_reason') or 'session').casefold()}"
    if action == "PROBE":
        return "challenge_deepen" if meta.get("probe_kind") == "deepen" else "insufficient_answer"
    if action == "NEXT_QUESTION":
        intent = str(meta.get("intent") or "")
        if intent in {"GIVE_UP", "SKIP_QUESTION"}:
            return f"{intent.casefold()}_move_on"
        if intent in {"OFF_TOPIC", "CANDIDATE_ABORT"}:
            return "not_an_answer"
        sufficiency = str(meta.get("sufficiency_status") or "")
        if sufficiency == "SUFFICIENT":
            return "sufficient_answer"
        if sufficiency:
            # Not enough, but the one follow-up was used or the stage takes none.
            return "move_on_without_probe"
        return "next_question"
    return action.casefold() or "unknown"


def turn_flags(
    *,
    action: str,
    meta: dict[str, Any],
    answer: str,
    question: str,
    rubric: str,
    reply: str,
    checks: list[dict[str, Any]],
) -> list[str]:
    """Rule flags of one turn (codes in ``FLAG_LABELS``)."""
    flags: list[str] = []
    outcomes = {str(check.get("outcome")) for check in checks}
    if outcomes & {"reject"}:
        flags.append("guard_reject")
    if "error" in outcomes or meta.get("ack_reason") == "llm_error":
        flags.append("llm_error")
    if looks_like_injection(answer):
        flags.append("injection")
    intent = str(meta.get("intent") or "")
    answer_words = len(str(answer or "").split())
    if intent == "GIVE_UP" and answer_words > 40:
        flags.append("giveup_but_long")
    if intent == "ANSWER" and answer_words >= 12 and question:
        if not (_words(answer) & _words(f"{question} {rubric}")):
            flags.append("answer_off_question")
    if (
        str(action).upper() == "PROBE"
        and meta.get("generated_by") == "llm"
        and meta.get("probe_kind") != "deepen"
    ):
        aspect = str(meta.get("missing_aspect") or "")
        probe = reply.rsplit("\n\n", 1)[-1]
        if aspect and not (_words(probe) & _words(aspect)):
            flags.append("probe_off_target")
    if _SYSTEM_LEAK_RE.search(str(reply or "")):
        flags.append("system_leak")
    return flags


ADJUSTED_POINT_REASONS = ("unverified_quote", "no_quote", "negation_in_quote", "quote_reused")
LOW_GRADE = 4.0


def session_flags(turn_grades: list[dict[str, Any]], verdicts: dict[int, set[str]]) -> dict[str, float]:
    """Checks after grading, as session scores.

    ``turn_grades``: ``{turn_index, score, rubric_points}`` per graded question.
    ``verdicts``: turn index -> sufficiency verdicts given during the interview.

    * ``check.sufficient_but_low_grade``: questions the interview judged complete
      (no follow-up) that grading scored below 4/10.
    * ``check.grade_points_adjusted``: rubric points whose label the code changed
      because the grader's quote was missing, made up, negated or reused.
    """
    low = 0
    adjusted = 0
    for grade in turn_grades:
        score = grade.get("score")
        index = grade.get("turn_index")
        if (
            score is not None
            and float(score) < LOW_GRADE
            and "SUFFICIENT" in verdicts.get(int(index or -1), set())
        ):
            low += 1
        for point in grade.get("rubric_points") or []:
            if isinstance(point, dict) and point.get("adjusted") in ADJUSTED_POINT_REASONS:
                adjusted += 1
    return {
        "check.sufficient_but_low_grade": float(low),
        "check.grade_points_adjusted": float(adjusted),
        "check.graded_questions": float(len(turn_grades)),
    }
