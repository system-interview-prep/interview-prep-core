"""Chủ đề người phỏng vấn được phép gọi tên — còn cấu trúc buổi thì không.

Buổi thật 11/11: sau phần mở đầu, AI nói "Mình chuyển sang phần Xác thực CV và
Dự án, bạn xác nhận để mình bắt đầu." Hai lỗi trong một câu. "Xác thực CV &
Dự án" là nhãn taxonomy của planner (``question_selector``), không phải chủ đề
ứng viên nghe được; và "bạn xác nhận để mình bắt đầu" là giọng hệ thống xin
thao tác, không phải người phỏng vấn đang trò chuyện.

Nguyên tắc: stage, competency, agenda, thứ tự và số lượng câu hỏi là chuyện nội
bộ, ứng viên không cần biết. Người phỏng vấn chỉ gọi tên chủ đề theo cách ứng
viên tự gọi — tên dự án trong CV, tên công nghệ — hoặc nói chung chung.

Module này trả lời hai câu hỏi, dùng chung cho Chat, Voice và Video:

* ``spoken_topic``: chủ đề sắp hỏi được phép gọi là gì (None khi không có gì
  an toàn để gọi tên).
* ``transition_rejection_reason``: câu vừa sinh có lộ cấu trúc buổi phỏng vấn
  hay xin xác nhận kiểu hệ thống không. Nguồn đã sạch thì đây là lưới an toàn
  cho những lần model tự nghĩ ra nhãn.
"""

from __future__ import annotations

import re
import unicodedata

from src.modules.interviews.core.dialogue.policy import speakable_label

# --------------------------------------------------------------------------- #
# Khái niệm nội bộ: hệ thống tự đặt, không phải kỹ năng trong taxonomy
# --------------------------------------------------------------------------- #

# ``question_selector`` gắn ba conceptId này cho các lượt do chính hệ thống dựng
# (mở đầu, xác thực CV, tình huống STAR). Nhãn của chúng không bao giờ được đọc
# lên; ``application/progress/skill_display`` đặt tên khác cho trang tiến độ.
INTERNAL_CONCEPT_IDS = frozenset({"warmup", "cv-validation", "soft-skill.star"})

# Nhãn nội bộ đúng như planner ghi vào ``question_snapshot``.
_INTERNAL_LABELS = (
    "Giới thiệu & Khởi động",
    "Xác thực CV & Dự án",
    "Kỹ năng mềm & Tình huống (STAR)",
    "Hỏi đáp & Văn hóa doanh nghiệp",
    "Hỏi đáp & Tổng kết",
)

# Chủ đề nói được cho từng khái niệm nội bộ. "warmup" cố ý không có: phần mở đầu
# không bao giờ được báo trước như một chủ đề.
SPOKEN_TOPIC_VI: dict[str, str] = {
    "cv-validation": "dự án trong CV của bạn",
    "soft-skill.star": "cách bạn làm việc cùng team",
}
SPOKEN_TOPIC_EN: dict[str, str] = {
    "cv-validation": "a project from your CV",
    "soft-skill.star": "how you work with a team",
}


def _normalize_label(value: str) -> str:
    """Bỏ dấu câu và quy "&" về "và" để so nhãn không phụ thuộc cách viết."""
    text_value = unicodedata.normalize("NFC", str(value or "")).casefold()
    text_value = text_value.replace("&", " và ")
    text_value = re.sub(r"[^\w\s]+", " ", text_value)
    return re.sub(r"\s+", " ", text_value).strip()


def _label_fragments() -> frozenset[str]:
    """Nhãn nội bộ đã chuẩn hoá, kèm phần đầu trước "và" nếu đủ dài để nhận ra.

    "Xác thực CV & Dự án" cho cả "xác thực cv và dự án" lẫn "xác thực cv", nên
    model rút gọn nhãn vẫn bị bắt. Phần đầu dưới ba tiếng ("giới thiệu",
    "hỏi đáp") thì bỏ: đó là lời nói thường.
    """
    fragments: set[str] = set()
    for label in _INTERNAL_LABELS:
        normalized = _normalize_label(label)
        if not normalized:
            continue
        fragments.add(normalized)
        head = normalized.split(" và ", 1)[0].strip()
        if head and len(head.split()) >= 3:
            fragments.add(head)
    return frozenset(fragments)


INTERNAL_LABEL_FRAGMENTS = _label_fragments()

# Tên stage trong ``InterviewStage``. Chỉ bắt dạng viết hoa hoặc có gạch dưới:
# model nhắc lại stage thì chép nguyên dạng đó, còn "validate", "challenge"
# viết thường là từ tiếng Anh bình thường.
_STAGE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z])(?:WARM[_ -]?UP|DEEP[_ -]?DIVE|VALIDATE|CHALLENGE|BEHAVIORAL|CLOSING"
    r"|warm_up|deep_dive)(?![A-Za-z])"
)

# Từ vựng của người dựng buổi phỏng vấn, không phải của người phỏng vấn.
_PLAN_WORD_RE = re.compile(
    r"(?<!\w)(?:agenda|blueprint|rubric|barem|taxonomy|competency|"
    r"kịch bản (?:phỏng vấn|buổi)|kế hoạch (?:phỏng vấn|buổi)|lộ trình buổi|"
    r"quy trình phỏng vấn|khung phỏng vấn)(?!\w)",
    re.IGNORECASE,
)

# "vòng 2", "phần 3", "bước thứ hai": đánh số là để lộ kế hoạch. "phần cuối",
# "phần tiếp theo" thì không — người phỏng vấn thật vẫn nói vậy.
_NUMBERED_SECTION_RE = re.compile(
    r"(?<!\w)(?:phần|vòng|mục|bước|giai đoạn|chặng|section|stage|round|phase|part)\s+"
    r"(?:thứ\s+)?(?:\d+|[IVX]{1,4}(?![A-Za-z])|nhất|hai|ba|bốn|năm|sáu)(?!\w)",
    re.IGNORECASE,
)

# "câu 3/10", "còn 4 câu nữa": tiến độ nội bộ.
_PROGRESS_RE = re.compile(
    r"(?<!\w)(?:câu\s+(?:hỏi\s+)?(?:số\s+)?\d+\s*(?:/|trên)\s*\d+"
    r"|còn\s+\d+\s+câu|\d+\s*/\s*\d+\s+câu)(?!\w)",
    re.IGNORECASE,
)

# Xin ứng viên xác nhận/đồng ý để hệ thống chạy tiếp. Người phỏng vấn mời bằng
# "nhé", không bắt ai xác nhận.
_CONFIRM_RE = re.compile(
    r"(?<!\w)(?:xác nhận\s+(?:để|rồi|thì|là)"
    r"|(?:bạn|anh|chị|em)\s+(?:hãy\s+|vui lòng\s+)?xác nhận"
    r"|(?:vui lòng|xin|làm ơn)\s+(?:xác nhận|đồng ý)"
    r"|(?:trả lời|nhắn|gõ|bấm|nhấn|phản hồi)[^.?!]{0,24}?(?:để|thì)\s+"
    r"(?:mình|tôi|chúng ta|hệ thống)?\s*(?:bắt đầu|tiếp tục|sang)"
    r"|please confirm|confirm (?:to|before|that you)"
    r"|(?:reply|type|press)[^.?!]{0,24}?to (?:begin|continue|start))(?!\w)",
    re.IGNORECASE,
)


def names_internal_structure(text_value: str) -> str | None:
    """Lý do một câu để lộ cấu trúc buổi phỏng vấn, hoặc None.

    Áp dụng được cho mọi câu ứng viên sẽ đọc hoặc nghe: tên stage, nhãn nội bộ,
    từ vựng kế hoạch, số thứ tự phần và tiến độ đều không thuộc về lời của
    người phỏng vấn.
    """
    value = str(text_value or "")
    if not value.strip():
        return None
    if _STAGE_TOKEN_RE.search(value):
        return "stage_name"
    normalized = _normalize_label(value)
    if any(fragment in normalized for fragment in INTERNAL_LABEL_FRAGMENTS):
        return "internal_label"
    if _PLAN_WORD_RE.search(value):
        return "plan_vocabulary"
    if _NUMBERED_SECTION_RE.search(value):
        return "numbered_section"
    if _PROGRESS_RE.search(value):
        return "progress_disclosure"
    return None


def asks_for_confirmation(text_value: str) -> bool:
    """Câu đang xin ứng viên xác nhận để hệ thống đi tiếp."""
    return bool(_CONFIRM_RE.search(str(text_value or "")))


def transition_rejection_reason(text_value: str) -> str | None:
    """Vì sao một lời chuyển chủ đề không nói ra được, hoặc None."""
    value = str(text_value or "").strip()
    if not value:
        return "empty"
    if reason := names_internal_structure(value):
        return reason
    if asks_for_confirmation(value):
        return "asks_confirmation"
    if "?" in value:
        return "is_question"
    return None


def spoken_topic(
    *,
    concept_id: str | None = None,
    competency: str | None = None,
    project_name: str | None = None,
    is_vi: bool = True,
) -> str | None:
    """Chủ đề sắp hỏi, gọi theo cách ứng viên tự gọi; None khi không gọi tên được.

    Thứ tự: tên dự án trong CV (cụ thể nhất và là của ứng viên), rồi lời nói
    sẵn cho khái niệm nội bộ, rồi nhãn kỹ năng nếu đọc lên được. Không có gì
    hợp lệ thì trả None và câu chuyển nói chung chung — vẫn hơn đọc nhãn máy.
    """
    name = str(project_name or "").strip()
    if name and not names_internal_structure(name):
        return f"dự án {name}" if is_vi else f"the {name} project"

    key = str(concept_id or "").strip().casefold()
    table = SPOKEN_TOPIC_VI if is_vi else SPOKEN_TOPIC_EN
    if key in table:
        return table[key]
    if key in INTERNAL_CONCEPT_IDS:
        return None

    label = speakable_label(competency)
    if label and names_internal_structure(label):
        return None
    return label


def generic_transition(topic: str | None, is_vi: bool = True) -> str:
    """Lời chuyển dự phòng khi model nói sai hoặc không có chủ đề gọi được."""
    value = str(topic or "").strip()
    if is_vi:
        return (
            f"Tiếp theo mình hỏi bạn một câu về {value} nhé."
            if value
            else "Tiếp theo mình hỏi bạn một câu khác nhé."
        )
    return (
        f"Next, I'd like to ask you about {value}."
        if value
        else "Next, I have another question for you."
    )


__all__ = [
    "INTERNAL_CONCEPT_IDS",
    "INTERNAL_LABEL_FRAGMENTS",
    "SPOKEN_TOPIC_EN",
    "SPOKEN_TOPIC_VI",
    "asks_for_confirmation",
    "generic_transition",
    "names_internal_structure",
    "spoken_topic",
    "transition_rejection_reason",
]
