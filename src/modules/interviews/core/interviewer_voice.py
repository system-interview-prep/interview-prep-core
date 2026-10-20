"""Lớp diễn đạt: cách người phỏng vấn AI nói, tách khỏi việc quyết định nói gì.

Code của engine vẫn quyết định hành động (hỏi thêm, sang câu mới, kết thúc).
Module này chỉ lo phần lời nói:

* ``INTERVIEWER_STYLE_*``: một bộ văn phong dùng chung cho mọi prompt sinh lời
  nói với ứng viên (câu ghi nhận, câu hỏi thêm, câu giải thích), để giọng nhất
  quán và không mang giọng "chuyên gia phân tích" của phần chấm.
* Các nhóm câu dự phòng nhiều biến thể và ``pick_variant``: chọn theo mã buổi,
  tránh câu vừa dùng, để cùng một câu không lặp nguyên văn trong một buổi.
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

# --------------------------------------------------------------------------- #
# Văn phong dùng chung
# --------------------------------------------------------------------------- #

INTERVIEWER_STYLE_VI = """NHÂN VẬT VÀ VĂN PHONG (áp dụng cho mọi câu ứng viên sẽ đọc hoặc nghe):
- Bạn là một kỹ sư senior đang phỏng vấn trực tiếp: điềm tĩnh, thân thiện, nói như nói chuyện, không đọc văn bản.
- Xưng "mình", gọi ứng viên là "bạn". Không dùng "em", "anh", "chị", "quý vị".
- Mỗi câu dưới 25 từ. Không gạch đầu dòng, không ngoặc, không ký hiệu, không liệt kê bằng dấu hai chấm.
- Giữ thuật ngữ IT bằng tiếng Anh (cache, index, deadlock, API); không dịch thô.
- Không khen, không chê, không nói đúng hay sai, không nhắc điểm, tiêu chí hay barem.
- Không mở đầu bằng "Cảm ơn", "Câu hỏi tiếp theo là" hay "Chúng ta hãy".
- Không mở đầu câu hỏi bằng khuôn "Bạn … giúp mình … nhé"; không thêm "nhé" sau mỗi câu."""

INTERVIEWER_STYLE_EN = """PERSONA AND STYLE (applies to every line the candidate will read or hear):
- You are a senior engineer interviewing in person: calm, friendly, conversational, never reading out a document.
- Address the candidate as "you". Sentences under 25 words. No bullet points, brackets, symbols or colon lists.
- Keep technical terms as they are (cache, index, deadlock, API).
- Never praise, criticise or say right or wrong; never mention scores, criteria or rubrics.
- Do not start with "Thank you", "The next question is" or "Let's"."""

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

# Câu đáp ngắn trung tính: dùng khi câu AI viết bị loại, và cho những lượt không
# phải câu trả lời thật (quá ngắn, "không biết", lời chèn lệnh).
# Buổi thật 17/10: câu nào cũng mở bằng "Ừ/À/Ok" nghe thành khuôn, nên một nửa
# nhóm nói thẳng vào ý. Không nói "cách làm": câu trả lời có thể không có cách làm.
ACK_FALLBACK_VI: tuple[str, ...] = (
    "Ừm, mình hiểu ý bạn.",
    "Mình nắm được ý đó rồi.",
    "À, mình nắm được rồi.",
    "Mình hiểu rồi, cảm ơn bạn.",
    "Vâng, mình nắm ý rồi.",
    "Vậy là mình hình dung được rồi.",
    "Ok, rõ ý bạn rồi.",
    "Mình theo kịp ý bạn rồi.",
)
ACK_FALLBACK_EN: tuple[str, ...] = (
    "Okay, I follow you.",
    "Mm, I see what you mean.",
    "Right, that makes sense to me.",
    "Okay, got it.",
    "I see, thanks for explaining.",
)

# Sau lời tự giới thiệu (giai đoạn khởi động).
INTRO_ACK_VI: tuple[str, ...] = (
    "Cảm ơn bạn đã giới thiệu.",
    "Mình nắm sơ qua về bạn rồi.",
    "Vậy là mình biết sơ qua về bạn rồi.",
)
INTRO_ACK_EN: tuple[str, ...] = (
    "Thanks for the introduction.",
    "Great, I have a picture of your background now.",
)

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


# Cho lượt không phải câu trả lời: lời chèn lệnh, hỏi chuyện khác, xin dừng.
# Không nói "hiểu cách làm", vì ứng viên không nói cách làm nào.
# Không dùng "mình ghi nhận": buổi thật 17/10 cho thấy nghe như đang ghi điểm.
NON_ANSWER_ACK_VI: tuple[str, ...] = (
    "Ừm, mình nghe rồi.",
    "Mình nghe bạn rồi.",
    "Vâng, mình nghe rồi.",
)
NON_ANSWER_ACK_EN: tuple[str, ...] = (
    "Okay, I hear you.",
    "Noted.",
    "Alright, I hear you.",
)

# Khi ứng viên nói "không biết": an ủi nhẹ, không "ghi nhận", không nói đúng sai.
GIVE_UP_ACK_VI: tuple[str, ...] = (
    "Không sao đâu, câu đó không dễ.",
    "Không sao, phần này mình tạm để đó nhé.",
    "Được, không sao cả.",
)
GIVE_UP_ACK_EN: tuple[str, ...] = (
    "No problem, I've noted that.",
    "That's fine, let's leave this one.",
)

# Câu hỏi thêm dự phòng, mỗi câu một kiểu: vì sao, cách khác, ví dụ thực tế,
# khi lỗi hoặc tải lớn, đánh đổi, kiểm chứng. Câu đầu giữ nguyên câu cũ.
PROBE_FALLBACK_VI: tuple[str, ...] = (
    "Vì sao bạn chọn cách đó, và nó có hạn chế gì?",
    "Nếu làm lại, bạn có cân nhắc cách nào khác không?",
    "Bạn kể một tình huống thực tế bạn đã áp dụng cách này được không?",
    "Khi lỗi xảy ra hoặc tải tăng gấp mười, cách này sẽ ra sao?",
    "Đánh đổi lớn nhất của cách làm đó là gì?",
    "Bạn kiểm tra cách làm đó chạy đúng bằng cách nào?",
)
PROBE_FALLBACK_EN: tuple[str, ...] = (
    "Why that approach, and what are its downsides?",
    "If you did it again, would you consider another approach?",
    "Can you walk me through a real case where you used this?",
    "What happens to this approach when something fails or load grows tenfold?",
    "What is the biggest trade-off of doing it that way?",
    "How would you verify that it works as intended?",
)

SKIP_ACK_VI: tuple[str, ...] = (
    "Được, mình chuyển sang câu khác nhé.",
    "Không sao, mình qua câu tiếp nhé.",
    "Ok, mình để câu này lại, sang câu khác nhé.",
)
SKIP_ACK_EN: tuple[str, ...] = (
    "Sure, let's move on to another question.",
    "No problem, let's take the next one.",
)

REPEAT_PREFIX_VI: tuple[str, ...] = (
    "Mình nhắc lại câu hỏi nhé:",
    "Ừ, mình hỏi lại nhé:",
    "Không sao, câu hỏi là:",
)
REPEAT_PREFIX_EN: tuple[str, ...] = (
    "Sure, here is the question again:",
    "Of course, the question was:",
)

CLOSE_COMPLETED_VI: tuple[str, ...] = (
    "Vậy là mình đã đi hết các phần rồi. Cảm ơn bạn đã dành thời gian, báo cáo đang được tổng hợp.",
    "Mình hỏi đủ các phần rồi. Cảm ơn bạn nhé, báo cáo đánh giá sẽ có ngay sau đây.",
    "Buổi hôm nay đến đây là xong. Cảm ơn bạn, mình đang tổng hợp báo cáo cho bạn.",
)
CLOSE_COMPLETED_EN: tuple[str, ...] = (
    "That covers everything I wanted to ask. Thanks for your time, your report is being prepared.",
    "We've gone through all the parts. Thank you, your evaluation report will be ready shortly.",
)

TIMEOUT_VI: tuple[str, ...] = (
    "Mình hết giờ rồi. Cảm ơn bạn, các câu trả lời đã được lưu để làm báo cáo.",
    "Thời gian buổi hôm nay đã hết. Cảm ơn bạn nhé, báo cáo sẽ có ngay sau đây.",
)
TIMEOUT_EN: tuple[str, ...] = (
    "We're out of time. Thank you, your answers have been saved for the report.",
    "That's all the time we have today. Thanks, your report will be ready shortly.",
)

EARLY_STOP_VI: tuple[str, ...] = (
    "Mình dừng buổi phỏng vấn ở đây nhé. Cảm ơn bạn, báo cáo sẽ có ngay sau buổi này.",
    "Hôm nay mình tạm dừng ở đây. Cảm ơn bạn, bạn xem báo cáo ngay sau đây nhé.",
)
EARLY_STOP_EN: tuple[str, ...] = (
    "Let's stop the interview here. Thank you, your report will be ready right after.",
    "We'll pause here for today. Thanks, you'll see your report right after this.",
)

FAREWELL_VI: tuple[str, ...] = (
    "Cảm ơn bạn đã tham gia buổi luyện phỏng vấn hôm nay. Báo cáo đánh giá đang được tạo, bạn xem ngay sau đây nhé.",
    "Vậy mình kết thúc ở đây nhé. Cảm ơn bạn, báo cáo sẽ hiện ngay sau buổi này.",
    "Buổi hôm nay đến đây thôi. Cảm ơn bạn đã dành thời gian, mình đang chuẩn bị báo cáo.",
)
FAREWELL_EN: tuple[str, ...] = (
    "Thank you for practising with us today. Your evaluation report is being prepared; you'll see it right after this.",
    "Let's wrap up here. Thanks, your report will appear right after this session.",
)

# Kết thúc khi chưa đi hết kế hoạch (chat_runtime hạ trạng thái "hoàn thành").
# Không được nói "đã hoàn thành tất cả" cho buổi dừng sớm.
WRAP_TIMEOUT_PARTIAL_VI: tuple[str, ...] = (
    "Mình hết giờ rồi nên chưa kịp đi hết các phần theo kế hoạch. Cảm ơn bạn, những gì mình đã trao đổi đều có trong báo cáo.",
)
WRAP_TIMEOUT_PARTIAL_EN: tuple[str, ...] = (
    "We're out of time, so we didn't get through every planned topic. Thanks, everything we discussed is in your report.",
)
WRAP_ENDED_EARLY_PARTIAL_VI: tuple[str, ...] = (
    "Buổi hôm nay phải kết thúc sớm nên mình chưa đi hết các phần. Cảm ơn bạn, những gì đã trao đổi đều có trong báo cáo.",
)
WRAP_ENDED_EARLY_PARTIAL_EN: tuple[str, ...] = (
    "The session had to end early, so we didn't cover every topic. Thanks, everything we discussed is in your report.",
)
# Ứng viên bấm dừng.
USER_ENDED_VI: tuple[str, ...] = (
    "Mình dừng buổi phỏng vấn ở đây theo ý bạn nhé. Cảm ơn bạn đã dành thời gian.",
)
USER_ENDED_EN: tuple[str, ...] = (
    "We'll stop the interview here as you asked. Thank you for your time.",
)

LAST_QNA_CLOSING_VI: tuple[str, ...] = (
    "Cảm ơn bạn đã dành thời gian cho buổi phỏng vấn hôm nay. Mình đang tổng hợp báo cáo đánh giá, chúc bạn một ngày tốt lành!",
    "Mình kết thúc buổi ở đây nhé. Báo cáo của bạn đang được tổng hợp, chúc bạn một ngày tốt lành!",
)
LAST_QNA_CLOSING_EN: tuple[str, ...] = (
    "Thank you for your time today. The system is now compiling your evaluation report. Have a great day!",
    "Let's end here. Your report is being compiled. Have a great day!",
)

# Câu đệm lúc chờ, "chưa nghe rõ" và câu nhắc khi im lặng chỉ có ở giọng nói, nên
# nằm ở adapters/voice/answer_window.py: voice agent chạy riêng, không import core.

# Xin dừng. Bản chữ nêu đúng tên hai nút trong khung chat; bản nói không đọc tên
# nút trong ngoặc (nghe rất máy), chỉ nói hai lựa chọn như người thật.
ABORT_CONFIRMATION_VI = (
    "Bạn muốn dừng buổi phỏng vấn phải không? Bạn chọn “Kết thúc & xem báo cáo” để dừng, "
    "hoặc “Tiếp tục phỏng vấn” để làm tiếp nhé."
)
ABORT_CONFIRMATION_EN = (
    "Do you want to stop the interview? Choose “End & view report” to stop, "
    "or “Continue interview” to keep going."
)
ABORT_CONFIRMATION_SPOKEN_VI = (
    "Bạn muốn dừng ở đây phải không? Nếu dừng, bạn bấm kết thúc để xem báo cáo; "
    "còn muốn làm tiếp thì bấm tiếp tục."
)
ABORT_CONFIRMATION_SPOKEN_EN = (
    "Would you like to stop here? If so, press end to see your report, or press continue to keep going."
)


def abort_confirmation(is_vi: bool, *, spoken: bool) -> str:
    if spoken:
        return ABORT_CONFIRMATION_SPOKEN_VI if is_vi else ABORT_CONFIRMATION_SPOKEN_EN
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

_TRANSITION_SKILL_VI: tuple[str, ...] = (
    "Giờ mình chuyển sang {next} nhé.",
    "Mình đổi chủ đề một chút, sang {next}.",
    "Tiếp theo mình muốn nghe về {next}.",
    "Mình qua phần {next} một chút nhé.",
)
_TRANSITION_SKILL_EN: tuple[str, ...] = (
    "Let's switch to {next} now.",
    "Changing topics a little, on to {next}.",
    "Next, I'd like to hear about {next}.",
)
_TRANSITION_STAGE_VI: dict[str, tuple[str, ...]] = {
    "VALIDATE": (
        "Mình muốn nghe thêm về dự án trong CV của bạn.",
        "Giờ mình nói về dự án bạn đã làm nhé.",
    ),
    "DEEP_DIVE": (
        "Giờ mình đi vào phần kỹ thuật nhé.",
        "Mình chuyển sang vài câu kỹ thuật nhé.",
    ),
    "CHALLENGE": (
        "Mình thử một tình huống khó hơn một chút nhé.",
        "Giờ mình đưa ra một tình huống thực tế nhé.",
    ),
    "BEHAVIORAL": (
        "Phần cuối, mình hỏi về cách bạn làm việc nhóm nhé.",
        "Giờ mình nói một chút về cách bạn làm việc với mọi người nhé.",
    ),
}
_TRANSITION_STAGE_EN: dict[str, tuple[str, ...]] = {
    "VALIDATE": ("I'd like to hear more about a project from your CV.",),
    "DEEP_DIVE": ("Let's get into some technical questions.",),
    "CHALLENGE": ("Let me try a slightly harder scenario.",),
    "BEHAVIORAL": ("Lastly, a bit about how you work with a team.",),
}
# Sau lượt ứng viên nói "không biết" hoặc xin qua câu: không báo "khó hơn".
_HARDER_RE = re.compile(r"khó hơn|harder", re.IGNORECASE)
_TRANSITION_STAGE_SOFT_EN: dict[str, tuple[str, ...]] = {"CHALLENGE": ("Let's look at a practical scenario.",)}

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
    stage_pools = _TRANSITION_STAGE_VI if is_vi else _TRANSITION_STAGE_EN
    if nxt_stage and nxt_stage != prev_stage and nxt_stage in stage_pools:
        pool = stage_pools[nxt_stage]
        if after_struggle:
            pool = tuple(line for line in pool if not _HARDER_RE.search(line)) or (
                _TRANSITION_STAGE_SOFT_EN.get(nxt_stage, ()) if not is_vi else ()
            )
            if not pool:
                return ""
        return pick_variant(pool, f"{seed}:stage:{nxt_stage}")
    nxt_label = _speakable_label(next_competency)
    prev_label = _speakable_label(previous_competency)
    if nxt_label and prev_label and nxt_label.casefold() != prev_label.casefold():
        pool = _TRANSITION_SKILL_VI if is_vi else _TRANSITION_SKILL_EN
        return pick_variant(pool, f"{seed}:skill:{nxt_label}").format(next=nxt_label)
    return ""


def compose_speech(acknowledgement: str, question: str, transition: str = "") -> str:
    """Ghép câu ghi nhận, câu chuyển và câu hỏi.

    Câu ghi nhận là đoạn đầu (chat tách đoạn đầu ra làm câu ghi nhận); câu chuyển
    đứng ngay trước câu hỏi trong đoạn sau.
    """
    ask = " ".join(part for part in (transition.strip(), question.strip()) if part)
    return "\n\n".join(part for part in (acknowledgement.strip(), ask) if part)
