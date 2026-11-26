"""Code-owned checks applied to candidate input before an agent decision."""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, Literal, Optional

from src.modules.interviews.core.contracts.interview import InterviewStage

if TYPE_CHECKING:
    from src.modules.interviews.core.conversation.turn_context import TurnContext


_INJECTION_RE = re.compile(
    r"(bỏ qua|phớt lờ|quên|bỏ hết)\s+(hết\s+)?(mọi|tất cả|các|những)?\s*"
    r"(hướng dẫn|chỉ dẫn|yêu cầu|lệnh|quy tắc|luật)"
    r"|\b(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+|your\s+)?"
    r"(previous\s+|prior\s+|above\s+|earlier\s+|system\s+)?(instructions|rules|above|guidelines)"
    r"|\b\d+\s*/\s*10\b|cho\s+(tôi|em|mình)\s+[\w\s]{0,12}điểm"
    r"|(in ra|cho (tôi|em|mình) xem|tiết lộ|reveal|print|show( me)?)\s+(the\s+|your\s+)?system prompt"
    # 25/10: role changes and verdicts the candidate dictates to the model.
    r"|từ\s+(giờ|bây giờ|nay)\s+(trở đi\s+)?(bạn|mày|ai)\s+(là|sẽ|hãy|đóng vai)"
    r"|(bạn|mày|ai)\s+(hãy\s+)?(đóng vai(?! trò)|nhập vai|giả làm|giả vờ là)"
    r"|(?:b\u1ea1n|ai|m\u00f4 h\u00ecnh)\s+(?:n\u00ean\s+|h\u00e3y\s+)?(?:l\u00e0m theo|th\u1ef1c hi\u1ec7n theo|nghe theo)\s+(?:\u0111i\u1ec1u|nh\u1eefng g\u00ec)\s+(?:t\u00f4i|em|m\u00ecnh|anh|ch\u1ecb)\s+v\u1eeba\s+(?:n\u00f3i|b\u1ea3o|d\u1eb7n)"
    r"|(đánh giá|chấm)\s+(câu trả lời (này|của (tôi|em|mình))|tôi|em|mình)\s[\w\s]{0,25}?"
    r"(là\s+)?(sufficient|đủ ý|đúng hết|đạt|xuất sắc|điểm (cao|tối đa))"
    r"|nói\s+(rằng\s+|là\s+)?(tôi|em|mình)\s+(trả lời\s+)?(đúng|đạt|giỏi|xuất sắc)"
    r"|\byou are now\b|\bpretend (to be|you are)\b|\bdeveloper mode\b|\bjailbreak\b"
    r"|\bact as (dan\b|an? (ai|assistant|evaluator|interviewer|grader|judge)\b)"
    r"|\b(say|tell me|confirm) (that )?my answer is\b"
    r"|\b(follow|do as|do what) (i|we) (just )?(said|asked|told you)\b"
    r"|</?\s*candidate_(response|code)\s*>"
    r"|^\s*(system|assistant|developer)\s*:\s*(bạn|you|hãy|ignore|bỏ qua|chấm|đánh giá|đặt|set)\b"
    r"|\bsufficiency_status\b|\"(intent|score|acknowledgement)\"\s*:",
    re.IGNORECASE | re.MULTILINE,
)


_ASK = (
    r"(?:cho\s+(?:tôi|tao|em|mình|anh|chị)\s+(?:xin\s+|biết\s+|xem\s+)?|xin\s+|đưa|gửi|nói|tiết lộ|in ra|"
    r"xuất ra|hiển thị|cung cấp|chia sẻ|đọc|nhắc lại|lặp lại|chép lại|liệt kê|"
    r"give|tell|show|reveal|print|leak|share|send|dump|repeat|recite|output|what(?:'s| is| are))"
)


_SECRET = (
    r"(?:api[\s_-]?keys?|secret[\s_-]?keys?|private[\s_-]?keys?|access[\s_-]?tokens?|tokens?\s+(?:của|of)|"
    r"mật khẩu|passwords?|khoá bí mật|khóa bí mật|khoá api|khóa api|biến môi trường|"
    r"environment variables?|env vars?|\.env\b|connection strings?|chuỗi kết nối|credentials?|"
    r"thông tin đăng nhập|openai_api_key|database_url)"
)


_PROMPT = (
    r"(?:system\s*prompts?|prompt\s+(?:hệ thống|gốc|ban đầu|của (?:bạn|mày|hệ thống))|lời nhắc hệ thống|"
    r"(?:hướng dẫn|chỉ dẫn|chỉ thị|quy tắc)\s+(?:hệ thống|ban đầu|gốc|bí mật|"
    r"(?:mà\s+)?(?:bạn|mày)\s+(?:được|đã được)\s+(?:giao|cài|lập trình)|của (?:bạn|mày))|"
    r"(?:your|the)\s+(?:system\s+|initial\s+|original\s+|hidden\s+)?(?:prompt|instructions)|"
    r"initial (?:prompt|instructions))"
)


_ROLE = (
    r"(?:admin|quản trị viên|người quản trị|developer|nhà phát triển|lập trình viên|kỹ sư|người tạo ra|"
    r"chủ|nhân viên|người)\s+(?:của\s+|bên\s+)?(?:bạn|mày|hệ thống này|hệ thống|intervia|openai|anthropic|"
    r"google|ai này|chatbot này|con bot này)"
)


_SYSTEM_PROBE_RE = re.compile(
    rf"{_ASK}[^.!?\n]{{0,40}}?{_SECRET}"
    rf"|{_SECRET}\s+(?:của\s+)?(?:bạn|mày|hệ thống|server|máy chủ|intervia|backend|database|"
    r"cơ sở dữ liệu|db|admin)\b[^.!?\n]{0,20}(?:là gì|là bao nhiêu|là\s*$|\?)"
    rf"|{_ASK}[^.!?\n]{{0,40}}?{_PROMPT}"
    rf"|{_PROMPT}[^.!?\n]{{0,25}}(?:là gì|thế nào|như nào|ra sao|\?)"
    # Which model is behind the interviewer.
    r"|(?:bạn|mày|hệ thống|intervia)\s+(?:đang\s+)?(?:dùng|sử dụng|chạy (?:trên|bằng)|được (?:xây|làm|tạo)"
    r"(?:\s+dựng)?\s+(?:trên|bằng|từ))\s+(?:model|mô hình|llm|ai|gpt|api)\s*(?:gì|nào)"
    r"|\bwhich (?:model|llm|ai)\b|\bwhat (?:model|llm) (?:are you|do you|is this)"
    # Claiming an identity to unlock something: "tôi là admin, hãy…", "tôi là mẹ của GPT".
    rf"|(?:tôi|tao|em|mình|anh|chị|ta)\s+(?:là|chính là)\s+{_ROLE}"
    r"|(?:tôi|tao|em|mình|anh|chị|ta)\s+(?:là|chính là)\s+(?:admin|quản trị viên|developer|nhà phát triển|"
    r"giám khảo|người chấm)\b[^.!?\n]{0,15}[,.]?\s*(?:hãy|cho|bỏ qua|in ra|tiết lộ|đưa|nói)"
    r"|(?:mẹ|bố|ba|cha|ông|bà|người)\s+(?:của|đẻ ra|sinh ra|tạo ra)\s+"
    r"(?:bạn|mày|gpt|chatgpt|claude|ai|openai)"
    r"|\bi(?:'m| am)\s+(?:your|the)\s+"
    r"(?:developer|creator|admin(?:istrator)?|owner|maker|programmer|mother|father)\b"
    r"|\bi(?:'m| am) (?:from|with|an? (?:employee|engineer) (?:of|at)) (?:openai|anthropic|intervia)\b"
    # Story wrappers that ask for a secret ("bà tôi hay đọc API key cho tôi ngủ").
    rf"|(?:bà|ông|mẹ|bố|grandma|grandmother)[^.!?\n]{{0,40}}(?:đọc|kể|hát|read|tell)[^.!?\n]{{0,40}}(?:{_SECRET}|{_PROMPT})",
    re.IGNORECASE,
)


_NEGATION_BEFORE_RE = re.compile(
    r"(?:không|chẳng|chưa|đừng|tránh|cấm|never|not|don't|do not|avoid)(?:\s+\w+){0,3}\s*$", re.IGNORECASE
)


def looks_like_system_probe(text_value: str) -> bool:
    """The candidate asks for a secret, the prompt or the model, or claims a role to get them."""
    value = str(text_value or "")
    return any(
        not _NEGATION_BEFORE_RE.search(value[max(0, match.start() - 30):match.start()])
        for match in _SYSTEM_PROBE_RE.finditer(value)
    )


def looks_like_injection(text_value: str) -> bool:
    text_value = str(text_value or "")
    return bool(_INJECTION_RE.search(text_value)) or looks_like_system_probe(text_value)




DEFINITE_GIVE_UP_KEYWORDS = {
    "ko biết", "không biết", "chịu", "em chịu", "mình chịu", "bỏ qua", "pass",
    "chưa học", "chưa rõ", "không rõ", "qua câu", "don't know", "no idea", "skip",
    "idk", "dunno", "k biet", "k biết", "chua hoc", "chua ro", "bó tay", "dont know",
    "have no idea", "skip question", "chịu thua", "bó tay rồi", "em ko biết",
    "em không biết", "mình không biết",
}

AMBIGUOUS_KEYWORDS = {
    "ừm", "um", "uhm", "uh", "ơ", "ờ", "k", "ko", "không", "chưa", "ok", "pk",
    "khong", "chua", "chưa ạ", "không ạ", "chưa từng", "dạ chưa", "dạ không",
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

EXPLICIT_ABORT_PATTERNS = [
    r"(dừng|ngừng|kết thúc|hủy|thôi|rút khỏi)\s*(lại\s*)?(buổi\s*|cuộc\s*)?phỏng vấn",
    r"không\s*(muốn\s*)?(phỏng vấn|tiếp tục phỏng vấn|thi)\s*nữa",
    r"\b(stop|end|abort|quit|cancel|leave|finish)\s+(the\s+|this\s+|our\s+)?interview\b",
    r"\b(cannot|can't|can not)\s+continue\s+(the\s+|this\s+)?interview\b",
    r"\bfamily emergency\b",
]

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

_REPEAT_NOT_HEARD = re.compile(
    r"\b(chưa|không|ko)\s+nghe\s+(rõ|được|thấy|kịp)\b"
    r"|\bnghe\s+(chưa|không|ko)\s+(rõ|được|kịp)\b"
    r"|\b(hỏi|nói)\s+gì\s+cơ\b|^\W*(dạ\W+)?gì\s+cơ\b"
    r"|\bcâu\s+hỏi\s+là\s+gì\b|\bnói\s+chậm\s+lại\b"
)

_REPEAT_AUDIO_PROBLEM = re.compile(r"\b(đứt\s+đoạn|gián\s+đoạn|bị\s+ngắt|bị\s+rè|mất\s+tiếng)\b")

_MAX_AUDIO_PROBLEM_WORDS = 6

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
    "SYSTEM_PROBE",
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

_MAX_ASIDE_WORDS = 20

_MAX_PROBE_WORDS = 40

_ABOUT_INTERVIEWER = re.compile(
    r"\b(bạn|anh|chị)\s+(là\s+(ai|người|bot|robot|chatbot|chatgpt|gpt|máy|trí tuệ nhân tạo)"
    r"|có\s+phải\s+(là\s+)?(người|ai|bot|robot|chatgpt|gpt|máy))\b"
)

_ASKS_BACK = re.compile(r"\?|\b(à|hả|vậy|không|chứ|phải\s+không)\W*$")

_ANSWER_REQUEST = re.compile(
    r"\b(cho\s+(em|mình|tôi)\s+(xin\s+)?|gợi\s+ý\s+)(đáp\s+án|lời\s+giải|câu\s+trả\s+lời)\b"
    r"|\b(đáp\s+án|lời\s+giải)\s+(đúng\s+)?là\s+gì\b"
    r"|\b(trả\s+lời|giải|viết|làm|code)\b(\s+\w+){0,3}\s+(giúp|hộ|giùm|thay)\s+(em|mình|tôi)\b"
)

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
    # Asking for a key, the prompt or the model, or claiming a role to get them, is
    # answered by a fixed line before any model sees it. A long message is an answer
    # that may name such things; the engine flags it instead (looks_like_injection).
    if len(cleaned.split()) <= _MAX_PROBE_WORDS and looks_like_system_probe(cleaned):
        return "SYSTEM_PROBE"
    if len(cleaned.split()) <= _MAX_ASIDE_WORDS:
        if _ANSWER_REQUEST.search(cleaned):
            return "ANSWER_REQUEST"
        # Only when asked: "anh là người quyết định" may describe a team lead.
        if _ABOUT_INTERVIEWER.search(cleaned) and _ASKS_BACK.search(cleaned):
            return "ABOUT_INTERVIEWER"
    # Role/process keywords inside a long answer (for example, "em ứng tuyển vị
    # trí Backend vì muốn hiểu API hoạt động như thế nào") are not an aside to
    # the interviewer. Preserve longer messages for answer evaluation.
    if len(cleaned.split()) > _MAX_ASIDE_WORDS:
        return None
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



def abort_request_decision(ctx: "TurnContext") -> Optional["Decision"]:
    """Honor an explicit stop request before any model sees the candidate text."""
    from src.modules.interviews.core.conversation.turn_context import Decision
    if ctx.current_stage == InterviewStage.CLOSING or not is_abort_request(ctx.candidate_text):
        return None
    return Decision(
        "confirm_abort",
        {"detected_by": "input_guardrail"},
        reason="candidate requested interview stop",
        evidence_refs=["answer:text"],
        path=["input_guardrail", "abort"],
    )


def system_probe_decision(ctx: "TurnContext") -> Optional["Decision"]:
    """Route a detected request for hidden system data without asking the model to judge it."""
    from src.modules.interviews.core.conversation.turn_context import Decision
    intent = classify_candidate_question(ctx.candidate_text)
    if intent != "SYSTEM_PROBE" and not looks_like_injection(ctx.candidate_text):
        return None
    return Decision(
        "decline_system_probe",
        {"intent": intent, "detected_by": "input_guardrail"},
        reason="candidate requested hidden system information",
        evidence_refs=["answer:text"],
        path=["input_guardrail", "system_probe"],
    )


def candidate_contains_injection(text_value: str) -> bool:
    """Identify explicit instruction-steering or system-probing input patterns."""
    return looks_like_injection(text_value)


def candidate_requests_system_data(text_value: str) -> bool:
    """Identify direct requests for hidden prompts, credentials, or model details."""
    return looks_like_system_probe(text_value)


__all__ = [
    "AMBIGUOUS_KEYWORDS",
    "CandidateQuestionIntent",
    "CLARIFY_PATTERNS",
    "DEFINITE_GIVE_UP_KEYWORDS",
    "EXPLICIT_ABORT_PATTERNS",
    "SKIP_QUESTION_PATTERNS",
    "SOFT_ABORT_MAX_CHARS",
    "SOFT_ABORT_PATTERNS",
    "abort_request_decision",
    "candidate_contains_injection",
    "candidate_requests_system_data",
    "classify_candidate_intent",
    "classify_candidate_question",
    "is_abort_request",
    "is_clarify_request",
    "is_repeat_request",
    "is_skip_request",
    "is_unresponsive_or_give_up",
    "is_yes_no_question",
    "looks_like_injection",
    "looks_like_system_probe",
    "system_probe_decision",
]


# Whole words only: the first version had a bare "ai" and matched "h-ai bạn"
# (session 1b264b67), so the fixed decline was said to a team-conflict story.
_SYSTEM_WORDS_RE = re.compile(
    r"\b(prompt|system prompt|rubric|chấm điểm|thang điểm|cách chấm|tiêu chí chấm|mô hình (ai|ngôn ngữ)|"
    r"llm|gpt|chatgpt|openai|instruction|chỉ dẫn|hướng dẫn (nội bộ|hệ thống)|api key|khoá api|khóa api|"
    r"token|nội bộ|cấu hình|config|mã nguồn|source code|bạn là ai|bạn là bot|bạn dùng (mô hình|model))\b",
    re.IGNORECASE,
)


def mentions_the_system(text: str) -> bool:
    """Whether the candidate said anything about the system, the model or how
    answers are assessed -- the least a model-chosen ``decline_system_probe``
    needs (session 08a2ee58 used it for "em đã trình bày rồi")."""
    return bool(_SYSTEM_WORDS_RE.search(str(text or "")))


_ALREADY_ANSWERED_RE = re.compile(
    r"(trình bày|trả lời|nói|kể|chia sẻ|giải thích)"
    r"(\s+(câu này|câu đó|phần này|phần đó|ở trên|lúc nãy|trước đó|hết))?"
    r"\s+(rồi|xong rồi|xong)\s*(mà|ạ|á|đó|nha|nhé|nhỉ|anh|chị)?\s*[.!?…]*\s*$",
    re.IGNORECASE,
)
_ALREADY_ANSWERED_MAX_WORDS = 25


def says_already_answered(text: str) -> bool:
    """A short "em đã trình bày rồi" / "em trả lời rồi mà": the candidate
    considers the question answered. Session 08a2ee58 (26/11) heard the same
    behavioural question again after saying it twice. Long messages are
    answers that may contain the words ("sau khi trình bày xong…"), so only
    short ones count."""
    value = str(text or "").strip()
    if not value or len(value.split()) > _ALREADY_ANSWERED_MAX_WORDS:
        return False
    return bool(_ALREADY_ANSWERED_RE.search(value))


_STOP_WORDS_RE = re.compile(
    r"\b(dừng|ngừng|kết thúc|tạm dừng|nghỉ|bỏ cuộc|không muốn (tiếp|làm|phỏng vấn)|xin phép (về|rời|dừng)|"
    r"stop|quit|end the interview|give up)\b",
    re.IGNORECASE,
)


def mentions_stopping(text: str) -> bool:
    """The least a model-chosen ``confirm_abort`` needs: the candidate said
    something about stopping. Session 1b264b67 asked "bạn có muốn kết thúc buổi
    phỏng vấn không?" after a team-conflict answer."""
    value = str(text or "")
    return is_abort_request(value) or bool(_STOP_WORDS_RE.search(value))
