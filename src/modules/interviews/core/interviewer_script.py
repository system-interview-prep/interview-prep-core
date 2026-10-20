"""Kịch bản nói của người phỏng vấn AI: một nguồn duy nhất cho chat, voice và video.

Mọi câu soạn sẵn mà ứng viên đọc hoặc nghe nằm ở file này. Code ở nơi khác chỉ
quyết định khi nào nói câu nào; muốn đổi câu chữ thì sửa ở đây.

File chỉ chứa dữ liệu, không import gì, để voice agent (chạy như một tiến trình
riêng, không nạp package ``src``) cũng đọc được bằng đường dẫn file. Xem
``adapters/voice/answer_window.py``.

Văn phong (tài liệu: interview/INTERVIEWER_SCRIPT_UNIFIED_2026-10-20.md):

* Xưng "mình", gọi "bạn"; "mình" chỉ dùng cho người phỏng vấn.
* Mỗi câu dưới 25 từ; không khen, không chê, không nói đúng hay sai.
* Không dùng "hãy", "vui lòng", "ghi nhận".
* Câu dùng chung cho cả ba kênh không gắn với giác quan: không "nghe", "đọc",
  "gõ". Những câu chỉ có ở kênh nói nằm trong mục cuối file.
* Tên nút viết trong “ ”; bước làm sạch cho TTS bỏ dấu ngoặc khi đọc.

``tests/modules/interviews/test_interviewer_script.py`` kiểm tra các quy tắc này.
"""

# Câu nói để nguyên một dòng cho dễ đọc và dễ sửa.
# ruff: noqa: E501

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Mở đầu buổi (giống nhau cho chat, voice, video)
# --------------------------------------------------------------------------- #

OPENING_GREETING_VI = "Chào {name}, mình là người phỏng vấn AI của Intervia."
OPENING_GREETING_EN = "Hi {name}, I'm the AI interviewer from Intervia."
# {role}: " vị trí Backend Developer" hoặc rỗng; {length}: ", khoảng 25 phút" hoặc rỗng.
OPENING_FRAME_VI = "Hôm nay mình luyện phỏng vấn{role}{length}."
OPENING_FRAME_EN = "Today we'll practise an interview{role}{length}."
OPENING_ROLE_VI = " vị trí {job_title}"
OPENING_ROLE_EN = " for the {job_title} role"
OPENING_LENGTH_VI = ", khoảng {minutes} phút"
OPENING_LENGTH_EN = ", for about {minutes} minutes"
OPENING_AGENDA_VI = "Mình sẽ hỏi về {agenda}."
OPENING_AGENDA_EN = "I'll ask about {agenda}."
OPENING_START_VI = "Mình bắt đầu nhé."
OPENING_START_EN = "Let's begin."
OPENING_DEMO_VI = "Buổi demo ba phút, mình hỏi nhanh vài câu."
OPENING_DEMO_EN = "This is a three-minute demo, so I'll ask a few quick questions."

# Tên từng phần trong lời giới thiệu (WARM_UP là câu hỏi ngay sau; CLOSING báo riêng).
STAGE_PHRASES_VI: dict[str, str] = {
    "VALIDATE": "dự án trong CV",
    "DEEP_DIVE": "chuyên môn",
    "CHALLENGE": "vài tình huống",
    "BEHAVIORAL": "cách bạn làm việc nhóm",
}
STAGE_PHRASES_EN: dict[str, str] = {
    "VALIDATE": "a project from your CV",
    "DEEP_DIVE": "technical knowledge",
    "CHALLENGE": "a few scenarios",
    "BEHAVIORAL": "how you work with a team",
}

# Câu hỏi đầu tiên. Dự án trong CV được hỏi ngay sau (VALIDATE) nên không hỏi ở đây.
WARMUP_QUESTION_VI = (
    "Bạn giới thiệu ngắn về bản thân trong một, hai phút: "
    "bạn đang làm gì và vì sao quan tâm vị trí này?"
)
WARMUP_QUESTION_DEMO_VI = (
    "Bạn giới thiệu ngắn về bản thân trong khoảng ba mươi giây: "
    "hiện bạn đang làm gì và vì sao quan tâm vị trí này?"
)
WARMUP_QUESTION_EN = (
    "Please introduce yourself in a minute or two: "
    "what you do now and why this role interests you?"
)
WARMUP_QUESTION_DEMO_EN = (
    "Please introduce yourself in about thirty seconds: "
    "what you do now and why this role interests you?"
)

# --------------------------------------------------------------------------- #
# Ghi nhận câu trả lời
# --------------------------------------------------------------------------- #

# Dùng khi câu ghi nhận AI viết bị rào chắn loại. Buổi thật 17/10: câu nào cũng
# mở bằng "Ừ/À/Ok" nghe thành khuôn, nên một nửa nhóm nói thẳng vào ý.
# Không "cảm ơn" sau mỗi câu trả lời: nghe như sắp kết thúc buổi.
ACK_FALLBACK_VI: tuple[str, ...] = (
    "Ừm, mình hiểu ý bạn.",
    "Mình nắm được ý đó rồi.",
    "À, mình nắm được rồi.",
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

# Sau lời tự giới thiệu.
INTRO_ACK_VI: tuple[str, ...] = (
    "Mình nắm sơ qua về bạn rồi.",
    "Vậy là mình biết sơ qua về bạn rồi.",
    "Ok, mình hình dung được bạn đang làm gì rồi.",
)
INTRO_ACK_EN: tuple[str, ...] = (
    "Thanks for the introduction.",
    "Great, I have a picture of your background now.",
)

# Lượt không phải câu trả lời: lời chèn lệnh, hỏi chuyện khác, xin dừng.
NON_ANSWER_ACK_VI: tuple[str, ...] = (
    "Ừm, mình hiểu rồi.",
    "Vâng, mình nắm rồi.",
    "Ok, mình hiểu.",
)
NON_ANSWER_ACK_EN: tuple[str, ...] = (
    "Okay, understood.",
    "Noted.",
    "Alright, understood.",
)

# Ứng viên nói "không biết": an ủi nhẹ, không nói đúng sai.
GIVE_UP_ACK_VI: tuple[str, ...] = (
    "Không sao đâu, câu đó không dễ.",
    "Không sao, phần này mình tạm để đó.",
    "Được, không sao cả.",
)
GIVE_UP_ACK_EN: tuple[str, ...] = (
    "No problem, I've noted that.",
    "That's fine, let's leave this one.",
)

# Ứng viên xin qua câu.
SKIP_ACK_VI: tuple[str, ...] = (
    "Được, mình chuyển sang câu khác nhé.",
    "Không sao, mình qua câu tiếp.",
    "Ok, mình để câu này lại, sang câu khác nhé.",
)
SKIP_ACK_EN: tuple[str, ...] = (
    "Sure, let's move on to another question.",
    "No problem, let's take the next one.",
)

# --------------------------------------------------------------------------- #
# Hỏi lại, giải thích lại, hỏi thêm
# --------------------------------------------------------------------------- #

REPEAT_PREFIX_VI: tuple[str, ...] = (
    "Mình nhắc lại câu hỏi nhé:",
    "Ừ, mình hỏi lại:",
    "Không sao, câu hỏi là:",
)
REPEAT_PREFIX_EN: tuple[str, ...] = (
    "Sure, here is the question again:",
    "Of course, the question was:",
)

# Khi câu giải thích lại do AI viết bị loại.
CLARIFY_FALLBACK_VI = "Mình hỏi theo cách khác nhé: {question}"
CLARIFY_FALLBACK_EN = "Let me put it another way: {question}"

# Câu hỏi thêm dự phòng, mỗi câu một kiểu: vì sao, cách khác, ví dụ thực tế,
# khi lỗi hoặc tải lớn, đánh đổi, kiểm chứng.
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

# Bài code: không khen lời giải, không gắn vào một kỹ thuật cụ thể.
CODING_FALLBACK_PROBE_VI = (
    "Cách làm này đánh đổi điều gì, và nếu có ngoại lệ xảy ra giữa chừng thì nó xử lý ra sao?"
)
CODING_FALLBACK_PROBE_EN = (
    "What does this approach trade off, and how does it cope if an exception is raised halfway?"
)

# --------------------------------------------------------------------------- #
# Chuyển phần
# --------------------------------------------------------------------------- #

TRANSITION_SKILL_VI: tuple[str, ...] = (
    "Giờ mình chuyển sang {next} nhé.",
    "Mình đổi chủ đề một chút, sang {next}.",
    "Tiếp theo mình muốn tìm hiểu về {next}.",
    "Mình qua phần {next} một chút nhé.",
)
TRANSITION_SKILL_EN: tuple[str, ...] = (
    "Let's switch to {next} now.",
    "Changing topics a little, on to {next}.",
    "Next, I'd like to talk about {next}.",
)
TRANSITION_STAGE_VI: dict[str, tuple[str, ...]] = {
    "VALIDATE": (
        "Mình muốn tìm hiểu thêm về dự án trong CV của bạn.",
        "Giờ mình qua dự án bạn đã làm nhé.",
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
        "Giờ mình qua cách bạn làm việc với mọi người nhé.",
    ),
}
TRANSITION_STAGE_EN: dict[str, tuple[str, ...]] = {
    "VALIDATE": ("I'd like to talk more about a project from your CV.",),
    "DEEP_DIVE": ("Let's get into some technical questions.",),
    "CHALLENGE": ("Let me try a slightly harder scenario.",),
    "BEHAVIORAL": ("Lastly, a bit about how you work with a team.",),
}
# Sau lượt "không biết" hoặc xin qua câu, câu có "khó hơn" bị bỏ; bản tiếng Anh
# cần một câu thay thế vì nhóm chỉ có một câu.
TRANSITION_STAGE_SOFT_EN: dict[str, tuple[str, ...]] = {"CHALLENGE": ("Let's look at a practical scenario.",)}

# --------------------------------------------------------------------------- #
# Ứng viên hỏi ngược hoặc đáp lạc đề: trả lời rồi nhắc lại câu hỏi (mọi kênh)
# --------------------------------------------------------------------------- #

# {minutes}: thời lượng buổi.
QUESTION_RESPONSES_VI: dict[str, str] = {
    "SCORING": (
        "Mình chấm theo độ đúng, độ sâu và ví dụ thực tế. "
        "Chi tiết tiêu chí mình không chia sẻ trong lúc phỏng vấn."
    ),
    "PROCESS": (
        "Buổi này khoảng {minutes} phút, lần lượt qua mở đầu, CV, chuyên môn, tình huống, "
        "hành vi và hỏi đáp. Bạn muốn mình nhắc lại, giải thích thêm hay đổi câu thì cứ đề nghị; "
        "muốn dừng thì chọn “Kết thúc”."
    ),
    "ROLE_QA": "Câu này mình xin để dành đến phần hỏi đáp cuối buổi.",
    "ABOUT_INTERVIEWER": (
        "Mình là người phỏng vấn AI của Intervia, giúp bạn luyện phỏng vấn theo CV và JD. "
        "Kết quả buổi này chỉ để bạn luyện tập."
    ),
    "ANSWER_REQUEST": (
        "Mình không trả lời thay bạn được, vì buổi này là để bạn luyện tập. "
        "Bạn cứ chia sẻ những gì bạn biết, chưa chắc cũng không sao."
    ),
    # Buổi thật 17/10: lời đáp lạc đề từng bị trả lời như một câu hỏi ngoài lề.
    "OFF_TOPIC_REPLY": "Hình như mình chưa nắm được ý bạn cho câu này.",
    "OFF_TOPIC": "Câu này nằm ngoài buổi phỏng vấn nên mình xin phép không trả lời.",
}
QUESTION_RESPONSES_EN: dict[str, str] = {
    "SCORING": (
        "I grade on correctness, depth and real examples. "
        "I don't share the detailed criteria during the interview."
    ),
    "PROCESS": (
        "The interview takes about {minutes} minutes and goes through an opening, your CV, "
        "technical questions, a scenario, behavioural questions and a Q&A. Just ask if you want "
        "a question repeated, explained or swapped; to stop, choose “End”."
    ),
    "ROLE_QA": "Let's keep that for the Q&A at the end.",
    "ABOUT_INTERVIEWER": "I am Intervia's AI interviewer, here to help you practise against your CV and the job.",
    "ANSWER_REQUEST": "I cannot answer for you: this session is your practice. Share what you know, even if unsure.",
    "OFF_TOPIC_REPLY": "I'm not sure I followed your answer to this one.",
    "OFF_TOPIC": "That is outside this interview, so I will leave it there.",
}
QUESTION_RETURN_VI = "Mình hỏi lại nhé: {question}"
QUESTION_RETURN_EN = "Let me ask again: {question}"

# --------------------------------------------------------------------------- #
# Xin dừng (một câu cho mọi kênh; tên nút khớp hộp xác nhận của cả hai phòng)
# --------------------------------------------------------------------------- #

ABORT_CONFIRMATION_VI = (
    "Bạn muốn dừng buổi ở đây phải không? Nếu dừng, bạn chọn “Kết thúc & xem báo cáo”; "
    "muốn làm tiếp thì chọn “Tiếp tục phỏng vấn”."
)
ABORT_CONFIRMATION_EN = (
    "Do you want to stop the interview here? If so, choose “End & view report”; "
    "to keep going, choose “Continue interview”."
)

# --------------------------------------------------------------------------- #
# Kết thúc
# --------------------------------------------------------------------------- #

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

# Kết thúc khi chưa đi hết kế hoạch: không được nói "đã hoàn thành tất cả".
WRAP_TIMEOUT_PARTIAL_VI: tuple[str, ...] = (
    "Mình hết giờ rồi nên chưa kịp đi hết các phần. Cảm ơn bạn, những gì mình đã trao đổi đều có trong báo cáo.",
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

# --------------------------------------------------------------------------- #
# Chỉ có ở kênh nói (voice, video): do bản chất kênh, chat không cần
# --------------------------------------------------------------------------- #

# Nói khi Interview Core xử lý lâu; xoay vòng để không lặp sau mỗi câu.
PROCESSING_FILLERS_VI: tuple[str, ...] = (
    "Ừm, chờ mình một chút nhé.",
    "Ừm, để mình xem lại ý bạn vừa nói.",
    "Ok, bạn chờ mình vài giây nhé.",
    "Mình đang xem câu trả lời của bạn, chờ chút nhé.",
)
PROCESSING_FILLERS_EN: tuple[str, ...] = (
    "Got it. Give me a moment.",
    "Okay, let me think about that for a second.",
    "Right, one moment please.",
)

# Bản ghi rỗng hoặc quá nhiễu.
NOT_HEARD_VI: tuple[str, ...] = (
    "Mình chưa nghe rõ câu trả lời, bạn nói lại giúp mình được không?",
    "Hình như mình nghe chưa rõ, bạn nói lại giúp mình nhé.",
    "Tiếng hơi nhỏ, bạn nói lại câu trả lời được không?",
)
NOT_HEARD_EN: tuple[str, ...] = (
    "I didn't catch that. Could you say it again?",
    "Sorry, I missed that. Could you repeat it?",
)

# Bản ghi có chữ nhưng chất lượng quá thấp để chấm.
UNCLEAR_TRANSCRIPT_VI = "Mình nghe chưa đủ rõ câu vừa rồi. Bạn nói lại chậm và gần micro hơn một chút nhé."
UNCLEAR_TRANSCRIPT_EN = (
    "I could not hear that clearly enough. "
    "Could you say it again, a little slower and closer to the microphone?"
)

# Interview Core lỗi khi xử lý câu trả lời.
CORE_ERROR_VI = "Mình chưa xử lý được câu trả lời vừa rồi. Bạn nói lại giúp mình nhé."
CORE_ERROR_EN = "I could not process that answer. Could you say it again?"

# Nhắc một lần mỗi câu hỏi khi ứng viên im lặng lâu.
SILENCE_REPROMPT_VI = (
    "Bạn cứ từ từ suy nghĩ. Sẵn sàng thì bạn cứ trả lời tự nhiên; "
    "muốn nghe lại thì nói “nhắc lại câu hỏi”."
)
SILENCE_REPROMPT_EN = (
    "Take your time and answer naturally when you're ready. "
    "If you want to hear it again, just say “repeat the question”."
)

# Mời trả lời tự nhiên, nói trước câu hỏi đầu tiên.
TURN_TAKING_TIP_VI = "Bạn cứ trả lời tự nhiên. Muốn nghe lại thì bạn nói “nhắc lại câu hỏi”."
TURN_TAKING_TIP_EN = "Answer naturally. If you need to hear a question again, just say “repeat the question”."

# Ứng viên chen "ừm", "vâng" khi chưa bắt đầu trả lời.
BACKCHANNEL_CONTINUE_VI = "Vâng, bạn cứ tiếp tục nhé."
BACKCHANNEL_CONTINUE_EN = "Sure, go ahead."
# Ứng viên xin chờ ("đợi mình chút").
HOLD_ACK_VI = "Vâng, bạn cứ tiếp tục khi sẵn sàng nhé."
HOLD_ACK_EN = "Sure, continue when you're ready."
LISTENING_ACK_VI = "Vâng."
LISTENING_ACK_EN = "Sure."
