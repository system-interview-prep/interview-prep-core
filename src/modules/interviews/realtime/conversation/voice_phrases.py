"""Voice-only spoken feedback and turn-taking phrases.

These lines describe audio/transcription conditions and stay in the Voice adapter.
"""

# --------------------------------------------------------------------------- #

# Câu đệm chờ Core được giữ để tương thích nhưng tắt mặc định; bật bằng
# VOICE_PROCESSING_FILLER_ENABLED=true. Mặc định phòng chỉ hiển thị trạng thái xử lý.
PROCESSING_FILLERS_VI: tuple[str, ...] = (
    "Ừm, chờ mình một chút nhé.",
    "Ừm, để mình xem lại ý bạn vừa nói.",
    "Ok, bạn chờ mình vài giây nhé.",
    "Mình đang xem câu trả lời của bạn, chờ chút nhé.",
    "Để mình xem lại ý bạn một chút.",
    "Bạn chờ mình vài giây.",
    "Mình xem lại câu trả lời một chút nhé.",
    "À, chờ mình chút.",
)
ANSWER_PAUSE_NUDGES_VI: tuple[str, ...] = (
    "Mình vẫn đang nghe, bạn cứ nói tiếp nhé.",
    "Bạn cứ từ từ nói tiếp, mình đang nghe đây.",
    "Mình theo dõi đây, bạn cứ nói tiếp khi sẵn sàng nhé.",
    "Bạn cứ nói tiếp nhé, mình vẫn ở đây.",
    "Mình đang nghe bạn, cứ tiếp tục nhé.",
)
ANSWER_PAUSE_NUDGES_EN: tuple[str, ...] = (
    "I'm listening. Go ahead when you're ready.",
    "Take your time; I'm still with you.",
    "Go ahead, I'm listening.",
    "Whenever you're ready, keep going.",
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
