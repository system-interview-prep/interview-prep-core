"""Lớp diễn đạt: cách người phỏng vấn AI nói, tách khỏi việc quyết định nói gì.

Code của engine vẫn quyết định hành động (hỏi thêm, sang câu mới, kết thúc).
Module này chỉ lo phần lời nói:

* ``INTERVIEWER_STYLE_*``: một bộ văn phong dùng chung cho mọi prompt sinh lời
  nói với ứng viên (câu ghi nhận, câu hỏi thêm, câu giải thích), để giọng nhất
  quán và không mang giọng "chuyên gia phân tích" của phần chấm.
* ``pick_variant``: chọn một câu trong nhóm câu soạn sẵn theo mã buổi, tránh câu
  vừa dùng. Câu chữ của mọi nhóm nằm ở ``interviewer_script.py`` (một kịch bản
  chung cho chat, voice và video); module này import lại để giữ tên cũ.
* ``transition_line``: câu chuyển khi đổi kỹ năng hoặc giai đoạn.
* ``compose_speech``: ghép câu ghi nhận, câu chuyển và câu hỏi.

Chuẩn hoá văn bản trước khi đọc thành giọng nằm ở
``adapters/voice/language.py`` (``speakable_text``), vì voice agent chạy độc lập.

Mọi câu ở đây đều trung tính: không khen, không chê, không nói đúng hay sai, vì
người phỏng vấn không được để lộ kết quả chấm trong lúc phỏng vấn.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Sequence

# Câu chữ nằm ở interviewer_script.py; tên cũ được giữ để code khác import từ đây.
from src.modules.interviews.core.interviewer_script import (  # noqa: F401
    ABORT_CONFIRMATION_EN,
    ABORT_CONFIRMATION_VI,
    ACK_FALLBACK_EN,
    ACK_FALLBACK_VI,
    CLOSE_COMPLETED_EN,
    CLOSE_COMPLETED_VI,
    EARLY_STOP_EN,
    EARLY_STOP_VI,
    FAREWELL_EN,
    FAREWELL_VI,
    GIVE_UP_ACK_EN,
    GIVE_UP_ACK_VI,
    INTRO_ACK_EN,
    INTRO_ACK_VI,
    LAST_QNA_CLOSING_EN,
    LAST_QNA_CLOSING_VI,
    NON_ANSWER_ACK_EN,
    NON_ANSWER_ACK_VI,
    PROBE_FALLBACK_EN,
    PROBE_FALLBACK_VI,
    REPEAT_PREFIX_EN,
    REPEAT_PREFIX_VI,
    SKIP_ACK_EN,
    SKIP_ACK_VI,
    TIMEOUT_EN,
    TIMEOUT_VI,
    TRANSITION_SKILL_EN,
    TRANSITION_SKILL_VI,
    TRANSITION_STAGE_EN,
    TRANSITION_STAGE_SOFT_EN,
    TRANSITION_STAGE_VI,
    USER_ENDED_EN,
    USER_ENDED_VI,
    WRAP_ENDED_EARLY_PARTIAL_EN,
    WRAP_ENDED_EARLY_PARTIAL_VI,
    WRAP_TIMEOUT_PARTIAL_EN,
    WRAP_TIMEOUT_PARTIAL_VI,
)

# --------------------------------------------------------------------------- #
# Văn phong dùng chung
# --------------------------------------------------------------------------- #

INTERVIEWER_STYLE_VI = """NHÂN VẬT VÀ VĂN PHONG (áp dụng cho mọi câu ứng viên sẽ đọc hoặc nghe):
- Bạn là một kỹ sư senior đang phỏng vấn trực tiếp: điềm tĩnh, thân thiện, nói như nói chuyện, không đọc văn bản.
- Xưng "mình", gọi ứng viên là "bạn". Không dùng "em", "anh", "chị", "quý vị". "Mình" luôn là người phỏng vấn, không dùng để chỉ ứng viên.
- Mỗi câu dưới 25 từ. Không gạch đầu dòng, không ngoặc, không ký hiệu, không liệt kê bằng dấu hai chấm.
- Giữ thuật ngữ IT bằng tiếng Anh (cache, index, deadlock, API); không dịch thô.
- Không khen, không chê, không nói đúng hay sai, không nhắc điểm, tiêu chí hay barem.
- Không mở đầu bằng "Cảm ơn", "Câu hỏi tiếp theo là" hay "Chúng ta hãy".
- Không mở đầu câu hỏi bằng khuôn "Bạn … giúp mình … nhé"; không thêm "nhé" sau mỗi câu.
- Không dùng "hãy", "vui lòng", "ghi nhận". Lời này dùng chung cho phỏng vấn chat, voice và video nên không dùng "nghe", "đọc", "gõ"."""

INTERVIEWER_STYLE_EN = """PERSONA AND STYLE (applies to every line the candidate will read or hear):
- You are a senior engineer interviewing in person: calm, friendly, conversational, never reading out a document.
- Address the candidate as "you". Sentences under 25 words. No bullet points, brackets, symbols or colon lists.
- Keep technical terms as they are (cache, index, deadlock, API).
- Never praise, criticise or say right or wrong; never mention scores, criteria or rubrics.
- Do not start with "Thank you", "The next question is" or "Let's".
- The same words are used in chat, voice and video interviews, so avoid "hear", "read" and "type"."""

# Câu ghi nhận mẫu, tốt và xấu. Đo ngày 17/10 cho thấy câu ghi nhận kiểu "Bạn mô
# tả được…", "Bạn phân biệt…" bị chấm là máy móc: đó là giọng tóm tắt bài làm.
# Người phỏng vấn thật chủ yếu đáp rất ngắn, thỉnh thoảng nhắc gọn một chi tiết.
ACK_EXAMPLES_VI: tuple[tuple[str, str], ...] = (
    ("Mình dùng Redis để cache kết quả query, đặt TTL 5 phút cho từng key.", "À, vậy là cache hết hạn sau năm phút."),
    ("Image là bản thiết kế read-only, container là cái đang chạy từ image.", "Mình hình dung được rồi."),
    ("Em phụ trách phần thanh toán, tích hợp VNPay cho hệ thống đặt vé.", "À, vậy phần VNPay là bạn làm."),
    ("Em chưa dùng Kubernetes, mới dùng Docker Compose thôi.", "Vậy hiện bạn chủ yếu dùng Compose."),
    ("Em là sinh viên năm cuối, đang thực tập backend ở một công ty fintech.", "À, bạn đang thực tập mảng backend."),
    ("Em dùng SELECT FOR UPDATE trong transaction khi trừ tồn kho.", "À, vậy là khoá dòng khi trừ tồn kho."),
    ("Em đặt timeout, retry có backoff, lỗi nhiều thì bật circuit breaker.", "Ừ, có cả circuit breaker nữa."),
)
# Đo vòng 1: câu tóm tắt bài bị chấm máy móc. Đo vòng 2: câu chỉ là cụm từ khoá
# ("À, dùng k6.") hoặc quá cụt ("Ok.") bị chấm cụt, hờ hững.
ACK_BAD_EXAMPLES_VI: tuple[str, ...] = (
    "Bạn mô tả được Read committed không đọc dữ liệu chưa commit.",
    "Bạn phân biệt rebase viết lại lịch sử và merge tạo merge commit.",
    "Mình ghi nhận bạn đặt timeout ba giây cho mỗi lần gọi service.",
    "À, SELECT FOR UPDATE.",
    "À, dùng k6.",
    "Ok.",
    # Đo vòng 3: chép lại cụm từ của ứng viên, hoặc câu chưa trọn.
    "À, refresh token được giữ trong httpOnly cookie.",
    "À, bạn dùng benchmark để trao đổi riêng.",
    "Ừm, khi network partition xảy ra.",
    "Ừm, mình đang theo dõi mức cô lập.",
    # Đo vòng 4: tự kết luận chất lượng hoặc suy diễn thêm.
    "Ừm, thời gian truy vấn đã giảm rõ rệt.",
    "Ừm, phiên đăng nhập được gia hạn an toàn.",
    "Ừm, bạn đối chiếu được cả ba nguồn.",
    "À, vậy thao tác được bảo vệ xuyên suốt.",
    # Buổi thật 17/10: câu dự phòng nói "cách làm" cho lời tự giới thiệu.
    "Ừ, mình hiểu cách làm đó.",
)
ACK_EXAMPLES_EN: tuple[tuple[str, str], ...] = (
    ("I cache query results in Redis with a five-minute TTL per key.", "Ah, five minutes."),
    ("Image is the read-only template, the container is what runs.", "Mm, okay."),
    ("I owned the payment flow on the booking system.", "Oh, the payment part was yours."),
)
ACK_BAD_EXAMPLES_EN: tuple[str, ...] = (
    "You described that read committed avoids dirty reads.",
    "You distinguished rebase from merge correctly.",
)


def style_guide(is_vi: bool) -> str:
    return INTERVIEWER_STYLE_VI if is_vi else INTERVIEWER_STYLE_EN


def ack_examples_block(is_vi: bool) -> str:
    examples = ACK_EXAMPLES_VI if is_vi else ACK_EXAMPLES_EN
    header = (
        "Ví dụ câu ghi nhận (chỉ để cảm nhận văn phong, KHÔNG chép lại):"
        if is_vi
        else "Acknowledgement examples (for tone only, do NOT copy):"
    )
    lines = [f'- "{answer}" → "{ack}"' for answer, ack in examples]
    if is_vi:
        lines.append("- Không phải câu nào cũng mở bằng À/Ừ/Ok: thỉnh thoảng nói thẳng vào ý.")
    bad = ACK_BAD_EXAMPLES_VI if is_vi else ACK_BAD_EXAMPLES_EN
    bad_header = (
        "KHÔNG viết kiểu tóm tắt bài làm như sau:" if is_vi else "Do NOT summarise the answer like this:"
    )
    return "\n".join([header, *lines, bad_header, *[f'- "{line}"' for line in bad]])


# Từ đánh giá: một câu ghi nhận chứa các từ này đang chấm bài trước mặt ứng viên
# ("đúng rồi", "chưa đủ"), nên bị thay bằng câu dự phòng.
ACK_EVALUATIVE_PATTERNS: tuple[str, ...] = (
    r"\b(đúng rồi|đúng vậy|chính xác|rất tốt|tốt lắm|tốt quá|hay lắm|hay quá|tuyệt|xuất sắc|giỏi|chuẩn rồi|"
    r"ấn tượng|hoàn hảo|sai rồi|chưa đúng|không đúng|chưa đủ|chưa chính xác|thiếu ý|còn thiếu)\b",
    r"\b(correct|exactly right|great answer|excellent|perfect|impressive|well done|good job|wrong|"
    r"incorrect|not quite|incomplete)\b",
)


def is_evaluative(text_value: str) -> bool:
    return any(re.search(pattern, text_value, re.IGNORECASE) for pattern in ACK_EVALUATIVE_PATTERNS)


# Chỉ dùng cho câu ghi nhận: tính từ kết luận chất lượng ("an toàn", "rõ rệt") và
# kiểu khen năng lực "bạn … được". Đo vòng 4 cho thấy câu nói về "mục đích, kết
# quả" dễ trượt sang các từ này. Không áp cho câu hỏi ("lưu mật khẩu an toàn thế
# nào?" là câu hỏi hợp lệ).
ACK_JUDGMENT_PATTERNS: tuple[str, ...] = (
    r"\b(an toàn|rõ rệt|hiệu quả|ổn định|hợp lý|chặt chẽ|bài bản|xuyên suốt|đầy đủ|tối ưu|chuyên nghiệp|"
    r"thông minh|gọn gàng|mạnh mẽ|rất rõ|khá rõ|rõ ràng|chuẩn)\b",
    r"\bbạn\b(?:\s+\S+){0,4}?\s+được\b",
    r"\b(secure|efficient|effective|solid|robust|clean|smart|thorough|clearly|well[- ]designed)\b",
)


def is_judgmental(text_value: str) -> bool:
    return any(re.search(pattern, text_value, re.IGNORECASE) for pattern in ACK_JUDGMENT_PATTERNS)


# --------------------------------------------------------------------------- #
# Nhóm câu dự phòng và cách chọn
# --------------------------------------------------------------------------- #

ACK_MAX_WORDS = 10
# Câu ghi nhận dưới 4 từ ("Ok.", "À, dùng k6.") bị chấm là cụt.
ACK_MIN_WORDS = 4

_INTERJECTION_RE = re.compile(r"^\W*(?:à|ừ|ừm|ok|okay|vâng|dạ|ồ|oh|ah|mm|right)\b\W*", re.IGNORECASE)


# "Ừm, khi network partition xảy ra." — a subordinate clause with no main clause.
_INCOMPLETE_OPENING_RE = re.compile(r"^(?:khi|nếu|vì|do|để|mà|còn|với|hoặc|tuy|when|if|because|while)\b", re.IGNORECASE)


def is_incomplete_clause(text_value: str) -> bool:
    rest = _INTERJECTION_RE.sub("", str(text_value or "")).strip()
    return bool(_INCOMPLETE_OPENING_RE.match(rest)) and "," not in rest


def is_keyword_fragment(text_value: str) -> bool:
    """"À, SELECT FOR UPDATE." — just the candidate's terms read back, no sentence."""
    rest = _INTERJECTION_RE.sub("", str(text_value or "")).strip(" .!…")
    words = rest.split()
    if not words or len(words) > 4:
        return False
    latin = sum(bool(re.fullmatch(r"[A-Za-z0-9+#./_-]+", w.strip(",."))) for w in words)
    return latin / len(words) >= 0.5

# "Bạn mô tả…", "Mình ghi nhận bạn…": giọng tóm tắt bài làm của giám khảo.
_GRADER_OPENING_RE = re.compile(
    r"^\W*(?:(?:à|ừ|ừm|ok|vâng|dạ)\W+)?(?:"
    r"bạn\s+(?:đã\s+|có\s+)?(?:mô tả|phân biệt|nêu|trình bày|giải thích|phân tích|chỉ ra|liệt kê|đề cập|tóm tắt)"
    r"|mình\s+ghi\s+nhận\s+(?:là\s+)?bạn"
    r"|you\s+(?:described|distinguished|explained|mentioned|listed|outlined)"
    r")",
    re.IGNORECASE,
)


def _content_words(text_value: str) -> list[str]:
    return [w for w in re.findall(r"\w+", _normalize(text_value)) if len(w) >= 2]


def is_grader_style(text_value: str) -> bool:
    return bool(_GRADER_OPENING_RE.search(str(text_value or "")))


PARROT_MIN_RUN = 5


def _longest_common_run(a: list[str], b: list[str]) -> int:
    best = 0
    previous = [0] * (len(b) + 1)
    for x in a:
        current = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            if x == y:
                current[j] = previous[j - 1] + 1
                best = max(best, current[j])
        previous = current
    return best


def is_parroting(ack: str, answer: str) -> bool:
    """The acknowledgement copies a long stretch of the answer word for word.

    Buổi thật 17/10: đếm tỉ lệ từ trùng đã loại nhầm câu tốt ("À, hiện bạn đang
    thực tập vị trí PM."), vì câu ghi nhận tự nhiên nào cũng dùng lại từ của ứng
    viên. Chỉ coi là chép khi trùng nguyên một đoạn từ 5 từ liền nhau trở lên.
    """
    return _longest_common_run(_content_words(ack), _content_words(answer)) >= PARROT_MIN_RUN


_INJECTION_RE = re.compile(
    r"(bỏ qua|phớt lờ|quên)\s+(mọi|tất cả|các|những)?\s*(hướng dẫn|chỉ dẫn|yêu cầu|lệnh)"
    r"|ignore\s+(all|any|previous|the)\s+(previous\s+)?instructions"
    r"|\b\d+\s*/\s*10\b|cho\s+(tôi|em|mình)\s+[\w\s]{0,12}điểm|\bsystem prompt\b",
    re.IGNORECASE,
)


def looks_like_injection(text_value: str) -> bool:
    return bool(_INJECTION_RE.search(str(text_value or "")))


def abort_confirmation(is_vi: bool, *, spoken: bool = False) -> str:
    """Câu xin xác nhận dừng: một câu cho mọi kênh (``spoken`` giữ cho code cũ)."""
    return ABORT_CONFIRMATION_VI if is_vi else ABORT_CONFIRMATION_EN


def _normalize(text_value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(text_value or ""))).strip().casefold()


def pick_variant(pool: Sequence[str], seed: str, avoid: Iterable[str] = ()) -> str:
    """Chọn một biến thể theo ``seed``, bỏ qua các câu vừa dùng.

    Cùng ``seed`` luôn ra cùng câu (dễ kiểm thử, phát lại không đổi lời); seed
    gồm mã buổi và lượt nên các lượt khác nhau ra câu khác nhau.
    """
    if not pool:
        return ""
    avoided = {_normalize(line) for line in avoid if line}
    start = int(hashlib.sha256(str(seed).encode("utf-8")).hexdigest(), 16) % len(pool)
    for offset in range(len(pool)):
        candidate = pool[(start + offset) % len(pool)]
        if _normalize(candidate) not in avoided:
            return candidate
    return pool[start]


def recent_contains(text_value: str, recent: Iterable[str]) -> bool:
    """True khi ``text_value`` trùng nguyên văn một câu vừa nói."""
    needle = _normalize(text_value)
    return bool(needle) and any(needle == _normalize(line) for line in recent if line)


def first_paragraph(text_value: str) -> str:
    return str(text_value or "").split("\n\n", 1)[0].strip()


def last_paragraph(text_value: str) -> str:
    parts = [part.strip() for part in str(text_value or "").split("\n\n") if part.strip()]
    return parts[-1] if parts else ""


# --------------------------------------------------------------------------- #
# Câu chuyển và ghép lời nói
# --------------------------------------------------------------------------- #

# Sau lượt ứng viên nói "không biết" hoặc xin qua câu: không báo "khó hơn".
_HARDER_RE = re.compile(r"khó hơn|harder", re.IGNORECASE)

# Nhãn kỹ năng mặc định hoặc dạng mã máy thì không đọc lên được.
_GENERIC_COMPETENCIES = {"", "chuyên môn", "expertise", "hỏi đáp & văn hóa doanh nghiệp", "hỏi đáp & tổng kết"}


def _speakable_label(label: str | None) -> str | None:
    value = str(label or "").strip()
    if (
        not value
        or value.casefold() in _GENERIC_COMPETENCIES
        or len(value) > 40
        or re.search(r"[:_/]|\d{3,}", value)
    ):
        return None
    return value


def transition_line(
    *,
    previous_stage: str | None,
    next_stage: str | None,
    previous_competency: str | None,
    next_competency: str | None,
    is_vi: bool,
    seed: str,
    after_struggle: bool = False,
) -> str:
    """Câu chuyển khi đổi giai đoạn hoặc kỹ năng; cùng chủ đề thì không cần câu nối.

    ``after_struggle``: ứng viên vừa nói không biết hoặc xin qua câu. Buổi thật
    17/10: AI nói "Mình thử một tình huống khó hơn" ngay sau đó, nghe như trêu.
    """
    prev_stage = str(previous_stage or "").upper()
    nxt_stage = str(next_stage or "").upper()
    stage_pools = TRANSITION_STAGE_VI if is_vi else TRANSITION_STAGE_EN
    if nxt_stage and nxt_stage != prev_stage and nxt_stage in stage_pools:
        pool = stage_pools[nxt_stage]
        if after_struggle:
            pool = tuple(line for line in pool if not _HARDER_RE.search(line)) or (
                TRANSITION_STAGE_SOFT_EN.get(nxt_stage, ()) if not is_vi else ()
            )
            if not pool:
                return ""
        return pick_variant(pool, f"{seed}:stage:{nxt_stage}")
    nxt_label = _speakable_label(next_competency)
    prev_label = _speakable_label(previous_competency)
    if nxt_label and prev_label and nxt_label.casefold() != prev_label.casefold():
        pool = TRANSITION_SKILL_VI if is_vi else TRANSITION_SKILL_EN
        return pick_variant(pool, f"{seed}:skill:{nxt_label}").format(next=nxt_label)
    return ""


def compose_speech(acknowledgement: str, question: str, transition: str = "") -> str:
    """Ghép câu ghi nhận, câu chuyển và câu hỏi.

    Câu ghi nhận là đoạn đầu (chat tách đoạn đầu ra làm câu ghi nhận); câu chuyển
    đứng ngay trước câu hỏi trong đoạn sau.
    """
    ask = " ".join(part for part in (transition.strip(), question.strip()) if part)
    return "\n\n".join(part for part in (acknowledgement.strip(), ask) if part)
