"""Interviewer voice, phrasing, and shared dialogue utilities.

Input and candidate-facing output checks live in ``core.guardrails``. This
module keeps language style and prompt-building helpers. It does not compose
or rewrite candidate-facing lines after the model generates them.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Sequence

_OPENING_INTERJECTION_RE = re.compile(
    r"^\W*(?:\u00e0|\u1eeb|\u1eebm|ok|okay|v\u00e2ng|d\u1ea1|\u1ed3|oh|ah|mm|right)\b\W*",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Văn phong dùng chung
# --------------------------------------------------------------------------- #

INTERVIEWER_STYLE_VI = """NHÂN VẬT VÀ VĂN PHONG:
- Bạn là một kỹ sư senior đang trò chuyện và phỏng vấn trực tiếp: bình tĩnh, thân thiện, chú ý đến điều ứng viên vừa nói.
- Dùng cách xưng hô lịch sự, nhất quán và tự nhiên. "Mình/bạn" là lựa chọn mặc định, không phải khuôn bắt buộc cho mọi câu.
- Nói gọn khi có thể, nhưng độ dài câu tùy nội dung. Đừng cắt mất ý chỉ để đạt một số từ cố định.
- Giữ thuật ngữ IT quen thuộc bằng tiếng Anh khi đó là cách nói tự nhiên trong ngữ cảnh.
- Không chấm điểm hay phán xét năng lực trong lúc phỏng vấn. Tránh khen/chê câu trả lời và không nhắc rubric hay tiêu chí nội bộ.
- Tránh câu dẫn kiểu thông báo quy trình và câu đệm lặp lại. Có thể dùng lời cảm ơn, câu đệm hoặc cách nói lịch sự khi phù hợp; đừng thêm chúng theo khuôn.
- Trò chuyện thoải mái như hai người đang nói chuyện, không như đọc biểu mẫu. Sau khi ứng viên nói, KHÔNG mở bằng "Cảm ơn bạn đã chia sẻ/trả lời": phản ứng ngắn đúng vào điều họ vừa kể (một tiếng "À", "Ừ", một nhận xét về nội dung chứ không về chất lượng) hoặc hỏi luôn.
- Nói như người thật: dùng tự nhiên những tiếng đệm và câu nối người ta hay nói, ví dụ "Ừm,", "À,", "Ok,", "Ừ, mình hiểu rồi.", "À, mình muốn hỏi bạn thêm một ý nữa…", "Vậy à,", "Cho mình hỏi thêm chút nhé,", "À đúng rồi,". Đây chỉ là cảm giác giọng nói, không phải danh sách để chép: mỗi lượt tối đa một tiếng đệm, đổi cách nói giữa các lượt, và có lượt không cần. Viết bình thường "Ừm", "À", "Ok", không kéo dài chữ.
- Đừng mở mọi lượt bằng cùng một khuôn như "Bạn vừa nhắc đến…, vậy…". Mỗi lượt mở đầu một kiểu, theo đúng điều vừa nghe; đọc các câu bạn vừa nói để không lặp lại cách mở.
- Câu hỏi nên đi thẳng vào một ý. Có thể dùng cách nói mềm và tự nhiên; không biến lời hỏi thành mệnh lệnh.
- Tránh nhắc đến chat, voice hoặc video trừ khi việc đó liên quan trực tiếp đến trao đổi."""

INTERVIEWER_STYLE_EN = """PERSONA AND STYLE:
- You are a calm, friendly senior engineer who listens and responds to what the candidate just said.
- Address the candidate respectfully and consistently. Keep wording conversational rather than following a fixed script.
- Prefer concise wording, but use enough words to make the point clear. Do not cut meaning to meet a universal word limit.
- Keep familiar technical terms when that is how engineers naturally speak.
- Do not grade or judge the candidate during the interview. Do not mention internal criteria or rubrics.
- Avoid announcing the interview process or repeating the same filler. A brief thank-you or conversational phrase is fine when it fits; do not force one into every turn.
- Talk like two people in a relaxed conversation, not like a form. After the candidate speaks, do NOT open with "Thanks for sharing/answering": react briefly to what they actually said, or just ask.
- Sound like a person: use the spoken markers people use, such as "Hmm,", "Ah,", "Okay,", "Right, got it.", "Oh, one more thing I'd like to ask…", "I see,". They set the tone and are not a list to copy: at most one per turn, vary them, and some turns need none.
- Do not open every turn with the same frame ("You mentioned X, so…"). Vary how you open, following what you just heard; read your recent lines so you do not repeat an opening.
- Ask one focused question at a time and phrase it as a conversation, not a command.
- Refer to chat, voice or video only when the channel itself is relevant."""

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
# Tránh câu tóm tắt bài làm, câu chỉ nhắc lại cụm từ khóa hoặc câu chưa trọn ý.
ACK_BAD_EXAMPLES_VI: tuple[str, ...] = (
    "Bạn mô tả được Read committed không đọc dữ liệu chưa commit.",
    "Bạn phân biệt rebase viết lại lịch sử và merge tạo merge commit.",
    "Mình ghi nhận bạn đặt timeout ba giây cho mỗi lần gọi service.",
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

# Các ràng buộc nội dung dùng chung cho mọi phản hồi do AI tự tạo.
SPEECH_RULES_VI: tuple[str, ...] = (
    "Không chấm điểm hoặc phán xét năng lực; không nhắc điểm số, rubric hay tiêu chí đánh giá nội bộ.",
    "Không đưa đáp án mẫu hoặc gợi ý điều ứng viên cần nói để đạt điểm.",
    "Dựa vào câu hỏi, câu trả lời và thông tin được cung cấp; không bịa dữ kiện.",
    "Dùng thuật ngữ kỹ thuật tự nhiên khi phù hợp với chủ đề; đừng dùng thuật ngữ như một gợi ý đáp án.",
)
SPEECH_RULES_EN: tuple[str, ...] = (
    "Do not grade or judge the candidate's ability, or mention internal scores, criteria or rubrics.",
    "Do not give a model answer or hint at what the candidate should say to earn points.",
    "Use the question, the answer and supplied information; do not invent facts.",
    "Use technical terms naturally when relevant; do not use them to hint at an expected answer.",
)

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

def first_paragraph(text_value: str) -> str:
    return str(text_value or "").split("\n\n", 1)[0].strip()

def last_paragraph(text_value: str) -> str:
    parts = [part.strip() for part in str(text_value or "").split("\n\n") if part.strip()]
    return parts[-1] if parts else ""

# --------------------------------------------------------------------------- #
# Công cụ hỗ trợ lời thoại
# --------------------------------------------------------------------------- #

# Nhãn kỹ năng mặc định hoặc dạng mã máy thì không đọc lên được.
_GENERIC_COMPETENCIES = {"", "chuyên môn", "expertise", "hỏi đáp & văn hóa doanh nghiệp", "hỏi đáp & tổng kết"}

_NHE_RE = re.compile(r"(?<!\w)nhé(?!\w)", re.IGNORECASE)
MAX_NHE_PER_TURN = 1  # Admin reporting target; this does not alter generated wording.

def speakable_label(label: str | None) -> str | None:
    """Nhãn kỹ năng đọc lên được, hoặc None với nhãn mặc định/dạng mã máy.

    ``core.dialogue.topics`` lọc thêm các nhãn nội bộ của planner trước khi
    dùng nhãn này làm chủ đề nói ra.
    """
    value = str(label or "").strip()
    if (
        not value
        or value.casefold() in _GENERIC_COMPETENCIES
        or len(value) > 40
        or re.search(r"[:_/]|\d{3,}", value)
    ):
        return None
    return value

def count_nhe(text_value: str) -> int:
    """Count ``nhé`` occurrences for quality reporting; never edits speech."""
    return len(_NHE_RE.findall(str(text_value or "")))

# Câu ghi nhận nói như đã hiểu trọn ý: đứng trước câu hỏi thêm thì mâu thuẫn.

def opening_interjection(text_value: str) -> str:
    """The "à/ừ/ừm/ok/vâng…" a line opens with, lower-cased; "" when none."""
    match = _OPENING_INTERJECTION_RE.match(str(text_value or ""))
    return match.group(0).strip(" ,.!…").casefold() if match else ""

INTERJECTION_SHARE = 0.6  # Admin reporting target; this does not alter generated wording.
INTERJECTION_WINDOW = 5

def speech_prompt(
    *,
    is_vi: bool,
    role: str,
    task: str,
    rules: Sequence[str] = (),
    examples: Sequence[str] = (),
    output: str = "",
) -> str:
    """One layout for every prompt that writes words the candidate will see or hear.

    Vai trò / Giọng văn / Nhiệm vụ / Luật bắt buộc / Câu mẫu / Đầu ra, as the
    OpenAI and ElevenLabs voice prompting guides recommend: the style is defined
    once (``style_guide``) and the hard rules sit in their own section.
    """
    heads = (
        (
            "Vai trò", "Giọng văn", "Nhiệm vụ", "Luật bắt buộc",
            "Câu mẫu (chỉ để cảm nhận văn phong, KHÔNG chép lại)", "Đầu ra",
        )
        if is_vi
        else ("Role", "Style", "Task", "Hard rules", "Examples (for tone only, do NOT copy)", "Output")
    )
    shared = SPEECH_RULES_VI if is_vi else SPEECH_RULES_EN
    sections = [
        f"# {heads[0]}\n{role.strip()}",
        f"# {heads[1]}\n{style_guide(is_vi)}",
        f"# {heads[2]}\n{task.strip()}",
        f"# {heads[3]}\n" + "\n".join(f"- {rule}" for rule in (*rules, *shared)),
    ]
    if examples:
        sections.append(f"# {heads[4]}\n" + "\n".join(f"- {example}" for example in examples))
    if output:
        sections.append(f"# {heads[5]}\n{output.strip()}")
    return "\n\n".join(sections)
