"""Pure intent, pacing, and interviewer-response rules used by the turn engine."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, Literal, Optional, Sequence

from src.core.trace_logging import trace_event
from src.modules.interviews.core import speech_checks
from src.modules.interviews.core.interview_types import InterviewStage
from src.modules.interviews.core.interviewer_script import (
    CLARIFY_FALLBACK_EN,
    CLARIFY_FALLBACK_VI,
    CODING_FALLBACK_PROBE_EN,
    CODING_FALLBACK_PROBE_VI,
    QUESTION_RESPONSES_EN,
    QUESTION_RESPONSES_VI,
    QUESTION_RETURN_EN,
    QUESTION_RETURN_VI,
)
from src.modules.interviews.core.interviewer_voice import (
    ACK_FALLBACK_EN,
    ACK_FALLBACK_VI,
    ACK_MAX_WORDS,
    ACK_MIN_WORDS,
    INTRO_ACK_EN,
    INTRO_ACK_VI,
    PROBE_FALLBACK_EN,
    PROBE_FALLBACK_VI,
    breaks_house_style,
    first_question_only,
    hint_rejection_reason,
    is_evaluative,
    is_grader_style,
    is_incomplete_clause,
    is_judgmental,
    is_keyword_fragment,
    is_parroting,
    judges_difficulty,
    pick_variant,
    probe_rejection_reason,
    recent_contains,
    speech_v2_enabled,
)
from src.modules.interviews.core.language_policy import response_matches_locale


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

PROHIBITED_LEAK_PATTERNS = [
    r"\b(điểm|điểm số|thang điểm|barem|rubric|tiêu chí|bạn được|bạn đạt)\b",
    r"\b(score|scores|grade|grading|rubrics|criterion|criteria|points)\b",
    r"\b\d+\s*/\s*10\b",
    r"\b\d+\s*điểm\b",
]

# The first variant of each pool, kept as the name older callers and tests use.
SAFE_FALLBACK_PROBE_VI = PROBE_FALLBACK_VI[0]
SAFE_FALLBACK_PROBE_EN = PROBE_FALLBACK_EN[0]
_FIXED_PROBES = frozenset(
    {*PROBE_FALLBACK_VI, *PROBE_FALLBACK_EN, CODING_FALLBACK_PROBE_VI, CODING_FALLBACK_PROBE_EN}
)


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


def fallback_probe(is_vi: bool, seed: str = "", recent: Any = ()) -> str:
    """A fixed follow-up; varies by session and turn so it does not repeat verbatim."""
    pool = PROBE_FALLBACK_VI if is_vi else PROBE_FALLBACK_EN
    if not seed:
        return pool[0]
    return pick_variant(pool, f"{seed}:probe", recent)


def fallback_acknowledgement(
    is_vi: bool, seed: str = "", recent: Any = (), pool: Optional[Sequence[str]] = None
) -> str:
    if pool is None:
        pool = ACK_FALLBACK_VI if is_vi else ACK_FALLBACK_EN
    return pick_variant(pool, f"{seed}:ack", recent)


def acknowledgement_pool(is_vi: bool, stage: Any = None) -> Sequence[str]:
    """Câu dự phòng theo giai đoạn: lời tự giới thiệu có nhóm câu riêng."""
    if str(getattr(stage, "value", stage) or "").upper() == "WARM_UP":
        return INTRO_ACK_VI if is_vi else INTRO_ACK_EN
    return ACK_FALLBACK_VI if is_vi else ACK_FALLBACK_EN


# Câu giải thích lại câu hỏi: buổi thật 17/10 AI viết "Hiền không cần kể chuyện
# đánh nhau; hãy tập trung…", nghe như lên lớp. Chỉ được nói lại câu hỏi.
CLARIFY_MAX_WORDS = 45
_CLARIFY_LECTURING_RE = re.compile(
    r"\b(hãy|không cần|đừng|không nên|vui lòng|cần phải|phải tập trung|you should|don't|do not|please focus)\b",
    re.IGNORECASE,
)


def clarify_rejection_reason(
    text_value: str, *, is_vi: bool, question: str = "", answer: str = "", rubric: str = ""
) -> Optional[str]:
    value = str(text_value or "").strip()
    if not value:
        return "empty"
    if "\n" in value:
        return "multi_line"
    if len(value.split()) > CLARIFY_MAX_WORDS or len(re.findall(r"[.!?…]+(?:\s|$)", value)) > 2:
        return "too_long"
    if _CLARIFY_LECTURING_RE.search(value) or (speech_v2_enabled() and breaks_house_style(value)):
        return "lecturing"
    if is_evaluative(value) or is_judgmental(value) or judges_difficulty(value):
        return "evaluative"
    if not validate_probe_text(value):
        return "rubric_leak"
    # A restatement may not name what the rubric expects ("ví dụ dùng TTL hoặc xoá key").
    hint = hint_rejection_reason(value, question=question, answer=answer, rubric=rubric)
    if hint and hint != "leading_question":
        return hint
    if not response_matches_locale(value, is_vi=is_vi):
        return "wrong_language"
    return None


def clarify_fallback_text(question_prompt: str, *, is_vi: bool) -> str:
    """Fixed clarification used when the model reply is missing or rejected."""
    return (CLARIFY_FALLBACK_VI if is_vi else CLARIFY_FALLBACK_EN).format(question=question_prompt)


def validate_probe_text(probe_text: str) -> bool:
    """Kiểm tra câu hỏi probe có hợp lệ và không rò rỉ barem/rubric hay không."""
    cleaned = probe_text.strip()
    if not cleaned or len(cleaned) > 280:
        return False
    for pattern in PROHIBITED_LEAK_PATTERNS:
        if re.search(pattern, cleaned, re.IGNORECASE):
            return False
    return True


def probe_is_sayable(probe_text: str) -> bool:
    """The follow-up can be asked as written (speech v2: the shared guard)."""
    if not speech_v2_enabled():
        return validate_probe_text(probe_text)
    reason = probe_rejection_reason(probe_text, lambda value: not validate_probe_text(value))
    if reason:
        trace_event("interviewer", "probe_rejected", reason=reason, probe=str(probe_text)[:200])
    return reason is None


def checked_probe(
    raw_probe: str, *, is_vi: bool, question: str = "", answer: str = "", rubric: str = ""
) -> str | None:
    """The AI's follow-up as it can be asked, or None; each change goes to the admin view.

    Keeps the first question only (a second one is cut, recorded as a fix),
    then applies the same guard as ``probe_is_sayable`` plus the locale check.
    """
    raw = str(raw_probe or "").strip()
    if not raw:
        return None
    probe = first_question_only(raw)
    if probe != raw:
        speech_checks.record("probe", "fix", "extra_question", raw)
    if not speech_v2_enabled():
        reason = None if validate_probe_text(probe) else "rubric_leak"
    else:
        reason = probe_rejection_reason(
            probe,
            lambda value: not validate_probe_text(value),
            question=question,
            answer=answer,
            rubric=rubric,
        )
    if reason is None and not response_matches_locale(probe, is_vi=is_vi):
        reason = "wrong_language"
    if reason:
        trace_event("interviewer", "probe_rejected", reason=reason, probe=probe[:200])
        speech_checks.record("probe", "reject", reason, probe)
        return None
    return probe


ACKNOWLEDGEMENT_MAX_CHARS = 120

# Structured Outputs for the evaluation call (speech v2). The spoken field comes
# first: it is written before the verdict, and is the first thing a streamed
# reply could say. Lengths are generous; the guards apply the real limits.
EVALUATION_SCHEMA: Dict[str, Any] = {
    "name": "interviewer_turn",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "acknowledgement": {"type": "string", "maxLength": 160},
            "intent": {
                "type": "string",
                "enum": ["ANSWER", "CANDIDATE_ABORT", "SKIP_QUESTION", "GIVE_UP", "OFF_TOPIC"],
            },
            "sufficiency_status": {"type": "string", "enum": ["SUFFICIENT", "INSUFFICIENT", "AMBIGUOUS"]},
            "missing_aspect": {"type": "string", "maxLength": 400},
            "probe_question": {"type": "string", "maxLength": 280},
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
            "properties": {"ack_topic": {"type": "string", "maxLength": 80}, **inner["properties"]},
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


# How an LLM line may address the candidate other than "bạn". The room calls the
# candidate by name, which chat_runtime substitutes for "bạn" only, so "em",
# "anh", "chị" reached the candidate unchanged ("mình hiểu phần em phụ trách").
_CANDIDATE_PRONOUN_RE = re.compile(r"\b(anh\s*/\s*chị|anh chị|em|anh|chị)\b", re.IGNORECASE)


def normalize_candidate_pronoun(text_value: str, is_vi: bool = True) -> str:
    """Address the candidate as "bạn" in a Vietnamese LLM line, keeping sentence case."""
    if not is_vi or not text_value:
        return text_value

    def _swap(match: re.Match[str]) -> str:
        # "chị Hiền" is a title before a name, which the name substitution handles.
        following = match.string[match.end() :].lstrip()
        if following[:1].isupper():
            return match.group(0)
        return "Bạn" if match.group(0)[0].isupper() else "bạn"

    return _CANDIDATE_PRONOUN_RE.sub(_swap, text_value)


def acknowledgement_rejection_reason(
    raw: Any, is_vi: bool, *, recent: Any = (), answer: str = ""
) -> Optional[str]:
    """Why an LLM acknowledgement cannot be said as is, or None when it can.

    The reason is logged per turn so a live session shows which guard fired.
    """
    text_value = str(raw or "").strip()
    if not text_value:
        return "empty"
    if len(text_value) > ACKNOWLEDGEMENT_MAX_CHARS or "\n" in text_value:
        return "too_long"
    if "?" in text_value:
        return "is_question"
    if any(re.search(pattern, text_value, re.IGNORECASE) for pattern in PROHIBITED_LEAK_PATTERNS):
        return "rubric_leak"
    if is_evaluative(text_value):
        return "evaluative"
    if len(text_value.split()) > ACK_MAX_WORDS:
        return "too_many_words"
    if len(text_value.split()) < ACK_MIN_WORDS:
        return "too_curt"
    if is_grader_style(text_value):
        return "grader_style"
    if is_parroting(text_value, answer):
        return "parroting"
    if is_keyword_fragment(text_value):
        return "keyword_fragment"
    if is_incomplete_clause(text_value):
        return "incomplete"
    if is_judgmental(text_value):
        return "judgmental"
    # "À, bạn chưa nói đến race condition.": tells the candidate what the answer lacked.
    if hint_rejection_reason(text_value) == "verdict_hint":
        return "verdict_hint"
    if recent_contains(text_value, recent or ()):
        return "repeats_recent"
    if speech_v2_enabled() and breaks_house_style(text_value):
        return "style"
    if not response_matches_locale(text_value, is_vi=is_vi):
        return "wrong_language"
    return None


def safe_acknowledgement(
    raw: Any,
    is_vi: bool,
    *,
    seed: str = "",
    recent: Any = (),
    answer: str = "",
    pool: Optional[Sequence[str]] = None,
) -> str:
    """Return the evaluator's acknowledgement only if it is a short, neutral line.

    The acknowledgement is LLM output shaped by the candidate's own answer and
    is shown/spoken verbatim, so an injected answer could make the interviewer
    announce a score or say anything else. Anything long, multi-line, asking a
    question, matching the rubric-leak patterns, judging the answer ("đúng
    rồi", "chưa đủ"), curt, a keyword fragment, unfinished, or repeating a
    line just said falls back to a neutral line picked from a pool, so the
    fallback itself does not repeat either.
    """
    if acknowledgement_rejection_reason(raw, is_vi, recent=recent, answer=answer):
        return fallback_acknowledgement(is_vi, seed, recent or (), pool)
    return normalize_candidate_pronoun(str(raw or "").strip(), is_vi)


# Tập từ khóa CHẮC CHẮN LÀ BỎ CUỘC (Không bao giờ nhầm với Yes/No)
DEFINITE_GIVE_UP_KEYWORDS = {
    "ko biết",
    "không biết",
    "chịu",
    "em chịu",
    "mình chịu",
    "bỏ qua",
    "pass",
    "chưa học",
    "chưa rõ",
    "không rõ",
    "qua câu",
    "don't know",
    "no idea",
    "skip",
    "idk",
    "dunno",
    "k biet",
    "k biết",
    "chua hoc",
    "chua ro",
    "bó tay",
    "dont know",
    "have no idea",
    "skip question",
    "chịu thua",
    "bó tay rồi",
    "em ko biết",
    "em không biết",
    "mình không biết",
}

# Tập từ khóa trung tính / mập mờ (Chỉ nghi vấn nếu câu hỏi KHÔNG PHẢI Yes/No)
AMBIGUOUS_KEYWORDS = {
    "ừm",
    "um",
    "uhm",
    "uh",
    "ơ",
    "ờ",
    "k",
    "ko",
    "không",
    "chưa",
    "ok",
    "pk",
    "khong",
    "chua",
    "chưa ạ",
    "không ạ",
    "chưa từng",
    "dạ chưa",
    "dạ không",
}


def is_yes_no_question(question_prompt: str) -> bool:
    """Nhận diện câu hỏi dạng Yes/No hoặc câu hỏi khảo sát kinh nghiệm có/chưa."""
    if not question_prompt:
        return False
    q = question_prompt.lower().strip()
    yes_no_patterns = [
        r"^(bạn|em|mình)?\s*(đã|có|từng|có phải|đã từng)",
        r"(chưa|không|phải không|đúng không)\s*\??$",
        r"\b(have you|did you|is it|can you|are you|do you|were you)\b",
        r"\b(từng|đã từng|có bao giờ)\b",
    ]
    return any(re.search(p, q) for p in yes_no_patterns)


# Spoken in voice and shown in chat. It names the two buttons every room shows
# for this choice (room.chat.stopAndReport / room.abort.confirm and
# room.common.continueInterview), never where they sit: chat puts them in the
# bubble, the voice room in a dialog.
# ABORT_CONFIRMATION_VI/EN: interviewer_script, một câu cho mọi kênh.

# Abort phrases that name the interview itself: unambiguous at any length.
EXPLICIT_ABORT_PATTERNS = [
    r"(dừng|ngừng|kết thúc|hủy|thôi|rút khỏi)\s*(lại\s*)?(buổi\s*|cuộc\s*)?phỏng vấn",
    r"không\s*(muốn\s*)?(phỏng vấn|tiếp tục phỏng vấn|thi)\s*nữa",
    r"\b(stop|end|abort|quit|cancel|leave|finish)\s+(the\s+|this\s+|our\s+)?interview\b",
    r"\b(cannot|can't|can not)\s+continue\s+(the\s+|this\s+)?interview\b",
    r"\bfamily emergency\b",
]

# Generic "stop" phrasing. Technical answers say the same words ("service phải
# dừng lại", "we need to stop the consumer"), so these only count for a short
# message that is plausibly a request on its own.
SOFT_ABORT_PATTERNS = [
    # Xin dừng / xin nghỉ / em muốn dừng ... (first-person request)
    r"(xin|cho\s*(em|mình|tôi)|cho phép|(em|mình|tôi)\s*(muốn|xin|phải|cần|đành))\s*(phép\s*)?(dừng|nghỉ|out|thôi|rút|kết thúc)\b",
    # Có việc bận / việc gia đình / việc riêng đi kèm dừng/nghỉ
    r"(việc\s*(bận|gấp|đột xuất|gia đình|riêng)|bận\s*(rồi|quá|việc|gia đình))\b.*?\b(dừng|nghỉ|thôi|out|về|kết thúc)",
    r"\b(dừng|nghỉ|thôi|out)\b.*?\b(việc\s*(bận|gấp|gia đình|riêng)|bận)",
    # Dừng lại đây / không làm nữa
    r"(dừng\s*(lại\s*)?(đây|ở đây|tại đây|nhé|nha|ạ|thôi)|không\s*(tiếp tục|làm)\s*nữa)",
    # A bare "dừng" / "dừng lại" said as the whole turn: nothing to grade, and
    # the dialog lets the candidate take it back.
    r"^\W*(thôi\W+)?(dừng|ngừng)(\s+lại)?\W*$",
    # "kết thúc phiên được không": short only, since "kết thúc phiên đăng
    # nhập" is a technical answer.
    r"(dừng|ngừng|kết thúc|hủy)\s*(cái\s*)?phiên\b(?!\s*(đăng nhập|login|làm việc|người dùng|của user))",
    r"\b(i|we)\s+(want to|have to|need to|must)\s+(stop|quit|leave|exit|abort)\s*(now|here|early|today)?\s*[.!]*$",
    r"\b(cannot continue|can't continue|stop here)\b",
]
SOFT_ABORT_MAX_CHARS = 100

# "Qua câu" / "chuyển sang câu" is a skip only when it names another question
# ("câu khác", "câu tiếp", "câu này") or ends the request. Matching the bare
# words moved on in the middle of answers such as "em sẽ đi qua câu lệnh
# SELECT" or "chuyển sang câu lệnh EXPLAIN".
SKIP_QUESTION_PATTERNS = [
    r"(cho\s*(em|mình)\s*)?(xin\s*)?(đổi|qua|bỏ qua|chuyển|skip)\s*(sang\s+|qua\s+)?"
    r"((câu hỏi|câu)(\s+(khác|tiếp|sau|mới|kế|này)\b|\s*([.?!,]|$)"
    r"|\s+(đi|nhé|nha|nhá|ạ|được|giúp|giùm|với)\b)"
    r"|(chủ đề|phần)\s+khác\b)",
    r"(câu\s*này|phần\s*này)\s*(em|mình)?\s*(chưa|không)\s*(rõ|biết|làm)\b.*?"
    r"\b(câu khác|câu tiếp|chủ đề khác|qua câu|bỏ qua)",
    r"\b(next question|skip question|pass this|skip this question)\b",
]


CLARIFY_PATTERNS = [
    r"(giải thích|nhắc|nói lại|hỏi lại|giải thích lại|làm rõ)\s*(thêm|hộ|cho|rõ hơn|rõ|lại)?\s*(câu hỏi|ý|đề bài|chủ đề)",
    r"(ý|nghĩa)\s*(của\s*)?(bạn|anh|chị|câu hỏi)\s*(là|như thế nào|nghĩa là gì|là sao)",
    r"\b(chưa hiểu|không hiểu|chưa rõ)\s*(rõ\s*)?(câu hỏi|ý|đề|yêu cầu)\b",
    r"\b(could you|can you|please)\s*(clarify|explain|repeat)\b",
    r"\b(what do you mean|pardon|meaning of the question)\b",
    r"^(ý là sao|là sao ạ|nghĩa là sao|chưa hiểu ạ|chưa rõ ạ)\??$",
]


def is_clarify_request(text: str) -> bool:
    """Nhận diện mọi biến thể yêu cầu giải thích / làm rõ câu hỏi của ứng viên."""
    cleaned = text.strip().lower()
    return any(re.search(pattern, cleaned, re.IGNORECASE) for pattern in CLARIFY_PATTERNS)


# "Nghe chưa rõ", "gì cơ": the candidate did not get the question, so the
# reply is not an answer. Same rules as the voice agent's is_repeat_request
# (kept separate: the agent runs as its own script). Matched on the text with
# its accents: folded, "họ" (they) would read as "hộ" (please).
_REPEAT_NOT_HEARD = re.compile(
    r"\b(chưa|không|ko)\s+nghe\s+(rõ|được|thấy|kịp)\b"
    r"|\bnghe\s+(chưa|không|ko)\s+(rõ|được|kịp)\b"
    r"|\b(hỏi|nói)\s+gì\s+cơ\b|^\W*(dạ\W+)?gì\s+cơ\b"
    r"|\bcâu\s+hỏi\s+là\s+gì\b|\bnói\s+chậm\s+lại\b"
)
# "Bị ngắt" is also an answer ("kết nối DB bị ngắt thì em retry").
_REPEAT_AUDIO_PROBLEM = re.compile(r"\b(đứt\s+đoạn|gián\s+đoạn|bị\s+ngắt|bị\s+rè|mất\s+tiếng)\b")
_MAX_AUDIO_PROBLEM_WORDS = 6
# Also answer words ("em hỏi lại PM"): a request only when addressed to the interviewer.
_REPEAT_VERB = re.compile(r"\b(nói|nhắc|lặp|hỏi|đọc|nghe)\s+lại\b")
_REPEAT_CUE = re.compile(r"\b(giúp|giùm|hộ|nhé|nha|được\s+không|được\s+ko|câu\s+hỏi)\b|\bđi\W*$")
_REPEAT_ADDRESSEE = re.compile(r"^\W*((dạ|vâng|ơ)\W+)?(bạn|anh|chị|cho\s+(em|mình|tôi)|xin|làm\s+ơn)\b")
_MAX_REPEAT_REQUEST_WORDS = 14


def is_repeat_request(text: str) -> bool:
    """A short aside asking to hear the current question again."""
    spoken = unicodedata.normalize("NFC", text).lower().strip()
    words = len(re.findall(r"\w+", spoken))
    if not words or words > _MAX_REPEAT_REQUEST_WORDS:
        return False
    if _REPEAT_NOT_HEARD.search(spoken):
        return True
    if _REPEAT_AUDIO_PROBLEM.search(spoken):
        return words <= _MAX_AUDIO_PROBLEM_WORDS
    if not _REPEAT_VERB.search(spoken):
        return False
    return words <= 3 or bool(_REPEAT_CUE.search(spoken) or _REPEAT_ADDRESSEE.search(spoken))


def is_abort_request(text: str) -> bool:
    """Nhận diện ứng viên xin dừng phỏng vấn.

    Cụm có nhắc tới buổi phỏng vấn luôn được tính; cụm chung chung chỉ được
    tính khi tin nhắn ngắn, để câu trả lời kỹ thuật như "service phải dừng lại
    và rollback" không bị hiểu nhầm là xin dừng.
    """
    cleaned = text.strip().lower()
    if any(re.search(pattern, cleaned, re.IGNORECASE) for pattern in EXPLICIT_ABORT_PATTERNS):
        return True
    return len(cleaned) <= SOFT_ABORT_MAX_CHARS and any(
        re.search(pattern, cleaned, re.IGNORECASE) for pattern in SOFT_ABORT_PATTERNS
    )


def is_skip_request(text: str) -> bool:
    """Nhận diện mọi biến thể xin đổi hoặc bỏ qua câu hỏi của ứng viên."""
    cleaned = text.strip().lower()
    return any(re.search(pattern, cleaned, re.IGNORECASE) for pattern in SKIP_QUESTION_PATTERNS)


def classify_candidate_intent(candidate_text: str, question_prompt: str = "") -> str:
    """
    Phân loại ý định của ứng viên (Context-Aware Intent Classification).
    Trả về:
    - 'CANDIDATE_ABORT': Ứng viên xin dừng phỏng vấn vì lý do cá nhân (Case 7)
    - 'SKIP_REQUEST': Ứng viên xin đổi câu hỏi (Case 3)
    - 'CLARIFY_REQUEST': Ứng viên nhờ làm rõ câu hỏi (Clarification)
    - 'GIVE_UP': Bỏ cuộc, không biết, cộc lốc không hợp tác
    - 'VALID_ANSWER': Câu trả lời hợp lệ (kể cả Yes/No như 'Không', 'Chưa')
    """
    cleaned = candidate_text.strip().lower()

    # 1. Phát hiện xin dừng phỏng vấn sớm (Case 7: Voluntary Abort)
    if is_abort_request(cleaned):
        return "CANDIDATE_ABORT"

    # 2. Phát hiện xin đổi / bỏ qua câu hỏi (Case 3: Skip Request)
    if is_skip_request(cleaned) and len(cleaned) <= 150:
        return "SKIP_REQUEST"

    # 2.5. Phát hiện yêu cầu làm rõ câu hỏi (Clarification Request)
    if is_clarify_request(cleaned) and len(cleaned) <= 150:
        return "CLARIFY_REQUEST"

    # 3. Lớp 1: Chắc chắn là bỏ cuộc (không bao giờ nhầm với Yes/No)
    if cleaned in DEFINITE_GIVE_UP_KEYWORDS:
        return "GIVE_UP"

    give_up_phrases = [
        "không biết",
        "ko biết",
        "k biết",
        "k biet",
        "em chịu",
        "mình chịu",
        "bỏ qua",
        "pass câu",
        "chưa học",
        "don't know",
        "dont know",
        "no idea",
        "have no idea",
        "skip question",
        "bó tay",
        "chưa có kinh nghiệm",
        "chua co kinh nghiem",
    ]
    if (
        any(p in cleaned for p in give_up_phrases)
        and len(cleaned) <= 40
        and not is_yes_no_question(question_prompt)
    ):
        return "GIVE_UP"

    # 4. Lớp 2: Kiểm tra từ mập mờ ("không", "chưa", "ok", "ừm", ...)
    if cleaned in AMBIGUOUS_KEYWORDS:
        # Nếu câu hỏi là Yes/No ("Bạn từng dùng Kafka chưa?" -> "Chưa" / "Không")
        if is_yes_no_question(question_prompt):
            return "VALID_ANSWER"  # Hợp lệ! Không được tính là trượt/bỏ cuộc.
        else:
            return "GIVE_UP"  # Câu hỏi lý thuyết mà nói "Không" / "Ừm" -> Bỏ cuộc.

    # 5. Lớp 3: Câu trả lời quá ngắn (<= 3 ký tự)
    if len(cleaned) <= 3 and not is_yes_no_question(question_prompt):
        return "GIVE_UP"

    return "VALID_ANSWER"


def is_unresponsive_or_give_up(text: str, question_prompt: str = "") -> bool:
    """Nhận diện nhanh các câu trả lời bỏ cuộc, cộc lốc hoặc thiếu hợp tác (có nhận thức ngữ cảnh Yes/No)."""
    intent = classify_candidate_intent(text, question_prompt)
    return intent in ("GIVE_UP", "SKIP_REQUEST")


CandidateQuestionIntent = Literal[
    "SCORING",
    "PROCESS",
    "ROLE_QA",
    "OFF_TOPIC",
    "ABOUT_INTERVIEWER",
    "ANSWER_REQUEST",
    # Không phải câu hỏi: lời đáp lạc đề do AI phân loại ("dạ, nói tiếp đi anh").
    "OFF_TOPIC_REPLY",
]


def _has_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    """Whole-word match: "ot" must not fire inside "Spring Boot" or "hotfix"."""
    return any(re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) for phrase in phrases)


# Only short asides are classified this way; a long reply is an answer.
_MAX_ASIDE_WORDS = 20
# "Bạn là ai / bạn là AI à": a question about the interviewer itself.
_ABOUT_INTERVIEWER = re.compile(
    r"\b(bạn|anh|chị)\s+(là\s+(ai|người|bot|robot|chatbot|chatgpt|gpt|máy|trí tuệ nhân tạo)"
    r"|có\s+phải\s+(là\s+)?(người|ai|bot|robot|chatgpt|gpt|máy))\b"
)
_ASKS_BACK = re.compile(r"\?|\b(à|hả|vậy|không|chứ|phải\s+không)\W*$")
# Asking the interviewer to answer for the candidate.
_ANSWER_REQUEST = re.compile(
    r"\b(cho\s+(em|mình|tôi)\s+(xin\s+)?|gợi\s+ý\s+)(đáp\s+án|lời\s+giải|câu\s+trả\s+lời)\b"
    r"|\b(đáp\s+án|lời\s+giải)\s+(đúng\s+)?là\s+gì\b"
    r"|\b(trả\s+lời|giải|viết|làm|code)\b(\s+\w+){0,3}\s+(giúp|hộ|giùm|thay)\s+(em|mình|tôi)\b"
)
# Everyday topics with nothing to do with the interview.
_OFF_TOPIC_MARKERS = (
    "thời tiết",
    "bóng đá",
    "ăn cơm",
    "ăn trưa",
    "ăn tối",
    "ăn gì",
    "phim",
    "bài hát",
    "ca sĩ",
    "người yêu",
    "chính trị",
    "giá vàng",
    "chứng khoán",
    "xổ số",
    "tử vi",
    "chuyện cười",
    "du lịch",
    "mấy giờ rồi",
    "hôm nay thứ mấy",
)


def classify_candidate_question(text: str) -> CandidateQuestionIntent | None:
    """Classify safe questions addressed to the interviewer.

    Both a question cue and a narrow interview topic are required because valid
    technical answers can also contain question marks or words such as "why".
    """
    cleaned = re.sub(r"\s+", " ", text.casefold()).strip()
    if len(cleaned.split()) <= _MAX_ASIDE_WORDS:
        if _ANSWER_REQUEST.search(cleaned):
            return "ANSWER_REQUEST"
        # Only when asked: "anh là người quyết định" may describe a team lead.
        if _ABOUT_INTERVIEWER.search(cleaned) and _ASKS_BACK.search(cleaned):
            return "ABOUT_INTERVIEWER"
    asks_question = "?" in cleaned or any(
        cue in cleaned
        for cue in (
            "cho hỏi",
            "xin hỏi",
            "muốn hỏi",
            "là gì",
            "như thế nào",
            "bao nhiêu",
            "bao lâu",
            "được không",
            "không ạ",
        )
    )
    if not asks_question:
        return None

    if _has_phrase(
        cleaned,
        (
            "rubric",
            "rubik",
            "barem",
            "chấm điểm",
            "thang điểm",
            "điểm số",
            "tiêu chí đánh giá",
            "trọng số",
            "đánh giá gì",
        ),
    ):
        return "SCORING"
    if _has_phrase(
        cleaned,
        (
            "bao nhiêu câu",
            "bao lâu",
            "thời lượng",
            "quy trình phỏng vấn",
            "buổi phỏng vấn",
            "các vòng",
            "các phần",
            "đổi câu",
            "bỏ qua câu",
            "tạm dừng",
        ),
    ):
        return "PROCESS"
    if _has_phrase(
        cleaned,
        (
            "công việc",
            "công ty",
            "vị trí",
            "team",
            "đội ngũ",
            "mức lương",
            "lương",
            "phúc lợi",
            "ot",
            "dự án của công ty",
            "nhà tuyển dụng",
        ),
    ):
        return "ROLE_QA"
    if len(cleaned.split()) <= _MAX_ASIDE_WORDS and _has_phrase(cleaned, _OFF_TOPIC_MARKERS):
        return "OFF_TOPIC"
    return None


def candidate_question_response(
    intent: CandidateQuestionIntent,
    *,
    current_question: str,
    is_vi: bool,
    duration_minutes: int,
    spoken: bool = True,
) -> str:
    """Answer a meta question, then repeat the current question.

    Every room repeats it: after an aside the candidate should not have to
    scroll back (chat) or remember it (voice). ``spoken`` is kept for older
    callers and no longer changes the wording.
    """
    del spoken
    responses = QUESTION_RESPONSES_VI if is_vi else QUESTION_RESPONSES_EN
    answer = responses.get(str(intent), responses["OFF_TOPIC"]).format(minutes=duration_minutes)
    back = (QUESTION_RETURN_VI if is_vi else QUESTION_RETURN_EN).format(question=current_question)
    return f"{answer}\n\n{back}"


__all__ = [
    "_loads_llm_json",
    "TIME_THRESHOLDS",
    "PROHIBITED_LEAK_PATTERNS",
    "SAFE_FALLBACK_PROBE_VI",
    "SAFE_FALLBACK_PROBE_EN",
    "_FIXED_PROBES",
    "DEEPEN_ASPECT_VI",
    "DEEPEN_ASPECT_EN",
    "fallback_probe",
    "fallback_acknowledgement",
    "acknowledgement_pool",
    "CLARIFY_MAX_WORDS",
    "_CLARIFY_LECTURING_RE",
    "clarify_rejection_reason",
    "clarify_fallback_text",
    "validate_probe_text",
    "probe_is_sayable",
    "checked_probe",
    "ACKNOWLEDGEMENT_MAX_CHARS",
    "EVALUATION_SCHEMA",
    "evaluation_schema",
    "MAX_QNA_QUESTIONS",
    "QNA_BUDGET_SECONDS",
    "_JOB_TEXT_LIMIT",
    "format_job_context",
    "_PACING_REFERENCE_SECONDS",
    "pacing_seconds",
    "_CANDIDATE_PRONOUN_RE",
    "normalize_candidate_pronoun",
    "acknowledgement_rejection_reason",
    "safe_acknowledgement",
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
    "candidate_question_response",
]
