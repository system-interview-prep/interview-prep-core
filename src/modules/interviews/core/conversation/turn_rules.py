"""Pure intent, pacing, and interviewer-response rules used by the turn engine."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, Sequence

from src.modules.interviews.core.guardrails.input import (
    AMBIGUOUS_KEYWORDS,
    CLARIFY_PATTERNS,
    CandidateQuestionIntent,
    DEFINITE_GIVE_UP_KEYWORDS,
    EXPLICIT_ABORT_PATTERNS,
    SKIP_QUESTION_PATTERNS,
    SOFT_ABORT_MAX_CHARS,
    SOFT_ABORT_PATTERNS,
    _ABOUT_INTERVIEWER,
    _ANSWER_REQUEST,
    _ASKS_BACK,
    _MAX_ASIDE_WORDS,
    _MAX_AUDIO_PROBLEM_WORDS,
    _MAX_REPEAT_REQUEST_WORDS,
    _OFF_TOPIC_MARKERS,
    _REPEAT_ADDRESSEE,
    _REPEAT_AUDIO_PROBLEM,
    _REPEAT_CUE,
    _REPEAT_NOT_HEARD,
    _REPEAT_VERB,
    _has_phrase,
    classify_candidate_intent,
    classify_candidate_question,
    is_abort_request,
    is_clarify_request,
    is_repeat_request,
    is_skip_request,
    is_unresponsive_or_give_up,
    is_yes_no_question,
)
from src.modules.interviews.core.guardrails.output import (
    acknowledgement_rejection_reason,
    checked_probe,
    probe_is_sayable,
    receipt_rejection_reason,
    validate_probe_text,
    PROHIBITED_LEAK_PATTERNS,
)
from src.modules.interviews.core.contracts.interview import InterviewStage



def _loads_llm_json(raw: str) -> Any:
    """Parse an LLM JSON reply, tolerating a ```json fence around it."""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


TIME_THRESHOLDS = {
    InterviewStage.WARM_UP: 0.10,  # Chiếm 10% đầu
    InterviewStage.VALIDATE: 0.30,  # Chiếm đến 30% tổng thời gian
    InterviewStage.DEEP_DIVE: 0.75,  # Kéo dài từ 30% đến 75%
    InterviewStage.CHALLENGE: 0.85,  # Kéo dài đến 85%
    InterviewStage.BEHAVIORAL: 0.95,  # Kéo dài đến 95%
    InterviewStage.CLOSING: 1.00,  # 5% cuối cùng
}

#: The warm-up may run this many times its planned share of the session
#: before its follow-ups stop regardless of count (a slow, long-winded
#: introduction still gets room; an endless one does not).
WARM_UP_TIME_OVERRUN = 2.0


#: Function words of a Vietnamese follow-up (folded, no diacritics). What is
#: left after removing them is what the line *asks for*: "lý do quan tâm vị
#: trí" rather than "bạn có thể cho mình biết thêm về ... không".
_FOLLOW_UP_STOPWORDS = frozenset(
    "ban minh em anh chi co the cho biet them ve khong la gi nay cua o duoc va den voi nhe a "
    "vay nhi dieu cu hon mot chut it xiu ro ra noi chia se hoi thu nua cung da dang se thi ma "
    "rat nhung cac ngoai neu lam ke moi hay hoac toi tu bang toi roi nen de".split()
)
#: Two asks are the same ask when this share of their content words coincide.
WARM_UP_REPEAT_OVERLAP = 0.6


def _asked_terms(sentence: str) -> set[str]:
    folded = unicodedata.normalize("NFKD", str(sentence or "").lower())
    ascii_text = "".join(ch for ch in folded if not unicodedata.combining(ch)).replace("đ", "d")
    return {t for t in re.findall(r"[a-z0-9]+", ascii_text) if t not in _FOLLOW_UP_STOPWORDS}


def _question_sentences(text_value: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.?!…])\s+", str(text_value or "").strip()) if p.strip()]
    asks = [p for p in parts if p.endswith("?")]
    return asks or parts


def follow_up_repeats(follow_up: str, previous: Sequence[str]) -> str | None:
    """Which earlier follow-up of this question the new one asks again, or None.

    Session e52c0757 (26/11): "Bạn có thể cho mình biết thêm về lý do bạn
    quan tâm đến vị trí này không?" was asked five times, each a little
    reworded ("lý do cụ thể", "rõ hơn"). A question sentence of the new line
    whose content words overlap an earlier line's question sentence by
    :data:`WARM_UP_REPEAT_OVERLAP` is the same ask. A different point
    ("bạn học năm mấy?") shares nothing and passes.
    """
    asks = [a for a in (_asked_terms(s) for s in _question_sentences(follow_up)) if len(a) >= 2]
    if not asks:
        return None
    for index, earlier in enumerate(previous, 1):
        for prior in (_asked_terms(s) for s in _question_sentences(earlier)):
            if len(prior) < 2:
                continue
            for ask in asks:
                if len(ask & prior) / len(ask | prior) >= WARM_UP_REPEAT_OVERLAP:
                    return f"follow_up:{index}"
    return None


# The warm-up's name for the same check (e0848ee, 09c2110).
warm_up_follow_up_repeats = follow_up_repeats


def warm_up_follow_ups_exhausted(ctx) -> str | None:
    """Why the warm-up must move on now instead of asking again, or None.

    Deterministic bounds, all policy rather than judgement: a follow-up that
    asks what an earlier follow-up of this warm-up already asked (the
    candidate answered it, however they answered -- asking a third time is
    the defect, not the cure), the blueprint's ``warm_up.max_follow_ups``
    (follow-ups already asked on this turn -- stored as CLARIFY messages,
    counted in ``clarify_count``), and the stage's time share times
    :data:`WARM_UP_TIME_OVERRUN`. The model still decides *what* to ask
    while follow-ups remain; it can no longer decide to ask forever.
    """
    if ctx.current_stage != InterviewStage.WARM_UP:
        return None
    repeat = str(getattr(ctx, "warm_up_repeat", "") or "")
    if repeat:
        return f"repeated_follow_up:{repeat}"
    limit = int(getattr(ctx.blueprint.warm_up, "max_follow_ups", 2))
    used = int(getattr(ctx, "clarify_count", 0) or 0)
    if used >= limit:
        return f"follow_ups:{used}/{limit}"
    target = max(1, int(getattr(ctx, "target_duration_seconds", 0) or 0))
    elapsed_ratio = float(getattr(ctx, "elapsed_seconds", 0) or 0) / target
    allowed_ratio = TIME_THRESHOLDS[InterviewStage.WARM_UP] * WARM_UP_TIME_OVERRUN
    if elapsed_ratio > allowed_ratio:
        return f"time:{elapsed_ratio:.2f}>{allowed_ratio:.2f}"
    return None


# What a deepening probe (challenge tier, sufficient answer) pushes on.
DEEPEN_ASPECT_VI = (
    "Ứng viên đã trả lời đủ ý chính. Hỏi sâu thêm MỘT bậc về đúng điều họ vừa nói, "
    "chẳng hạn vì sao họ chọn cách đó thay vì cách khác, hoặc giới hạn của nó trong chính "
    "bối cảnh họ kể. Không dựng tình huống mới mà họ chưa nhắc tới. Không hỏi lại điều họ đã trả lời."
)
DEEPEN_ASPECT_EN = (
    "The candidate covered the main points. Push ONE level deeper on exactly what they said, "
    "such as why that approach rather than another, or its limits in the very context they "
    "described. Do not invent a new scenario they did not raise. Do not ask again what they already answered."
)
#: Clarifications of one question before the interview moves on. Session
#: 0e3c14c6 (26/11) rephrased the same follow-up three times while the
#: candidate said they did not understand it and asked to move on.
MAX_CLARIFIES_PER_QUESTION = 2
#: Interviewer replies on one question that do not move the interview --
#: repeat, redirect, clarify, answering the candidate's question, declining.
#: Session 08a2ee58 (26/11) answered a behavioural answer with a redirect, a
#: decline and a repeat in a row; each is bounded alone, but not together.
MAX_NON_ADVANCING_PER_QUESTION = 3
NON_ADVANCING_ACTIONS = (
    "repeat_question", "redirect_off_topic", "clarify_question",
    "answer_candidate_question", "decline_system_probe",
)


def non_advancing_exhausted(ctx) -> bool:
    if ctx.current_stage in (InterviewStage.WARM_UP, InterviewStage.CLOSING):
        return False
    used = int((getattr(ctx, "session_state", None) or {}).get("non_advancing_count") or 0)
    return used >= MAX_NON_ADVANCING_PER_QUESTION


# Structured output for the legacy evaluation call. Lengths are generous; the
# candidate-facing output guards apply the real limits.
EVALUATION_SCHEMA: Dict[str, Any] = {
    "name": "interviewer_turn",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "acknowledgement": {"type": "string"},
            "intent": {
                "type": "string",
                "enum": [
                    "ANSWER",
                    "CANDIDATE_ABORT",
                    "SKIP_QUESTION",
                    "GIVE_UP",
                    "OFF_TOPIC",
                    "SYSTEM_PROBE",
                ],
            },
            "sufficiency_status": {"type": "string", "enum": ["SUFFICIENT", "INSUFFICIENT", "AMBIGUOUS"]},
            "missing_aspect": {"type": "string", "maxLength": 400},
            "probe_question": {"type": "string"},
            "score": {"type": "number", "minimum": 1, "maximum": 10},
        },
        "required": [
            "acknowledgement",
            "intent",
            "sufficiency_status",
            "missing_aspect",
            "probe_question",
            "score",
        ],
    },
}


def evaluation_schema(*, with_topic: bool = False) -> Dict[str, Any]:
    """The evaluation schema; with ``ack_topic`` first when acknowledgements are framed by code."""
    if not with_topic:
        return EVALUATION_SCHEMA
    inner = EVALUATION_SCHEMA["schema"]
    return {
        "name": "interviewer_turn_topic",
        "schema": {
            **inner,
            "properties": {"ack_topic": {"type": "string"}, **inner["properties"]},
            "required": ["ack_topic", *inner["required"]],
        },
    }

# Closing Q&A budget: at most this many candidate questions, and at most
# QNA_BUDGET_SECONDS (scaled like the other pacing thresholds) in total.
MAX_QNA_QUESTIONS = 2
QNA_BUDGET_SECONDS = 180
_JOB_TEXT_LIMIT = 1500


def format_job_context(job: dict[str, Any] | None, is_vi: bool) -> str:
    """Render the posting facts the closing Q&A may rely on, and nothing else."""
    job = job or {}
    lines: list[str] = []

    def add(label_vi: str, label_en: str, value: Any) -> None:
        if value not in (None, "", [], {}):
            lines.append(f"- {label_vi if is_vi else label_en}: {value}")

    add("Công ty", "Company", job.get("company_name"))
    add("Vị trí", "Position", job.get("title"))
    add("Địa điểm", "Location", job.get("location"))
    add("Hình thức làm việc", "Work mode", job.get("work_mode"))
    add("Loại hợp đồng", "Employment type", job.get("employment_type"))
    add("Cấp bậc", "Seniority", job.get("seniority"))
    if job.get("salary_min") or job.get("salary_max"):
        low, high = job.get("salary_min") or "?", job.get("salary_max") or "?"
        salary = f"{low} - {high} {job.get('salary_currency') or ''}".strip()
        add("Mức lương", "Salary", salary)
    elif job.get("salary_negotiable"):
        add("Mức lương", "Salary", "thoả thuận" if is_vi else "negotiable")
    add("Mô tả công việc", "Job description", str(job.get("description") or "")[:_JOB_TEXT_LIMIT])
    add("Yêu cầu", "Requirements", str(job.get("requirements") or "")[:_JOB_TEXT_LIMIT])
    return "\n".join(lines) or ("(Không có thông tin tuyển dụng)" if is_vi else "(No posting information)")

# Pacing thresholds below are tuned for a 25-minute session.
_PACING_REFERENCE_SECONDS = 25 * 60


def pacing_seconds(default_seconds: int, target_duration_seconds: int) -> int:
    """Scale a pacing threshold down for sessions shorter than 25 minutes.

    The fixed values (30s hard timeout, 90s cutoff, 60s closing + 180s
    behavioral reserve) consume a short session whole: a 3-minute demo jumped
    to BEHAVIORAL after ~1 minute and never asked a technical question. Sessions
    of 25 minutes or more keep the original values unchanged.
    """
    scaled = round(default_seconds * target_duration_seconds / _PACING_REFERENCE_SECONDS)
    return max(1, min(default_seconds, scaled))


def normalize_candidate_pronoun(text_value: str, is_vi: bool = True) -> str:
    """Compatibility hook: keep natural pronouns chosen by the speech model.

    Prompts recommend neutral ``mình/bạn`` by default, but post-processing must
    not rewrite a contextually natural address into a fixed phrase.
    """
    return text_value



__all__ = [
    "_loads_llm_json",
    "TIME_THRESHOLDS",
    "PROHIBITED_LEAK_PATTERNS",
    "DEEPEN_ASPECT_VI",
    "DEEPEN_ASPECT_EN",
    "validate_probe_text",
    "probe_is_sayable",
    "checked_probe",
    "EVALUATION_SCHEMA",
    "evaluation_schema",
    "MAX_QNA_QUESTIONS",
    "QNA_BUDGET_SECONDS",
    "_JOB_TEXT_LIMIT",
    "format_job_context",
    "_PACING_REFERENCE_SECONDS",
    "pacing_seconds",
    "normalize_candidate_pronoun",
    "acknowledgement_rejection_reason",
    "receipt_rejection_reason",
    "DEFINITE_GIVE_UP_KEYWORDS",
    "AMBIGUOUS_KEYWORDS",
    "is_yes_no_question",
    "EXPLICIT_ABORT_PATTERNS",
    "SOFT_ABORT_PATTERNS",
    "SOFT_ABORT_MAX_CHARS",
    "SKIP_QUESTION_PATTERNS",
    "CLARIFY_PATTERNS",
    "is_clarify_request",
    "_REPEAT_NOT_HEARD",
    "_REPEAT_AUDIO_PROBLEM",
    "_MAX_AUDIO_PROBLEM_WORDS",
    "_REPEAT_VERB",
    "_REPEAT_CUE",
    "_REPEAT_ADDRESSEE",
    "_MAX_REPEAT_REQUEST_WORDS",
    "is_repeat_request",
    "is_abort_request",
    "is_skip_request",
    "classify_candidate_intent",
    "is_unresponsive_or_give_up",
    "CandidateQuestionIntent",
    "_has_phrase",
    "_MAX_ASIDE_WORDS",
    "_ABOUT_INTERVIEWER",
    "_ASKS_BACK",
    "_ANSWER_REQUEST",
    "_OFF_TOPIC_MARKERS",
    "classify_candidate_question",
]
