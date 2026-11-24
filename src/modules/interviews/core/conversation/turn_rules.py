"""Pure intent, pacing, and interviewer-response rules used by the turn engine."""

from __future__ import annotations

import json
import re
from typing import Any, Dict

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

# What a deepening probe (challenge tier, sufficient answer) pushes on.
DEEPEN_ASPECT_VI = (
    "Ứng viên đã trả lời đủ ý chính. Hỏi sâu thêm MỘT bậc về đúng điều họ vừa nói: "
    "đánh đổi của cách đó, chuyện gì xảy ra khi tải tăng mạnh hoặc một thành phần lỗi, "
    "hoặc vì sao không chọn cách khác. Không hỏi lại điều họ đã trả lời."
)
DEEPEN_ASPECT_EN = (
    "The candidate covered the main points. Push ONE level deeper on exactly what they said: "
    "the trade-off of that approach, what happens under much higher load or when a part fails, "
    "or why not another approach. Do not ask again what they already answered."
)


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
