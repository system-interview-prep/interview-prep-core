# src/modules/interviews/core/interview_types.py
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional
from uuid import UUID
from pydantic import BaseModel, Field


class InterviewStage(str, Enum):
    WARM_UP = "WARM_UP"         # Giai đoạn 1: Chào hỏi, tạo nhịp
    VALIDATE = "VALIDATE"       # Giai đoạn 2: Kiểm tra tính trung thực của CV (Fast-fail check)
    DEEP_DIVE = "DEEP_DIVE"     # Giai đoạn 3: Đào sâu kỹ thuật cốt lõi theo JD (Core Competencies)
    CHALLENGE = "CHALLENGE"     # Giai đoạn 4: Bài toán tình huống / System Design mini
    BEHAVIORAL = "BEHAVIORAL"   # Giai đoạn 5: Kỹ năng mềm theo chuẩn STAR
    CLOSING = "CLOSING"         # Giai đoạn 6: Q&A ứng viên & Tạm biệt
    CLOSED = "CLOSED"           # Khóa session, sẵn sàng cho P4 chấm điểm


class TurnAction(str, Enum):
    PROBE = "PROBE"                 # Hỏi đào sâu câu hiện tại (tối đa 1 lần)
    NEXT_QUESTION = "NEXT_QUESTION" # Chuyển sang câu hỏi tiếp theo
    WRAP_UP = "WRAP_UP"             # Chuẩn bị kết thúc phiên
    TERMINATE = "TERMINATE"         # Dừng phỏng vấn ngay lập tức (Early-exit / Hết giờ)
    CONFIRM_ABORT = "CONFIRM_ABORT" # Kích hoạt Modal xác nhận dừng sớm từ phía ứng viên


class SessionExitReason(str, Enum):
    NORMAL_COMPLETION = "NORMAL_COMPLETION"       # Hoàn thành trọn vẹn các vòng
    HARD_TIMEOUT = "HARD_TIMEOUT"                 # Hết giờ toàn phiên
    FAST_FAIL_VALIDATION = "FAST_FAIL_VALIDATION" # Trượt thẩm định CV (Nghi vấn CV ảo)
    FAST_FAIL_TECH = "FAST_FAIL_TECH"             # Trượt 2 câu hỏi kỹ thuật tiên quyết
    CANDIDATE_ABORT = "CANDIDATE_ABORT"           # Ứng viên chủ động dừng


class CandidateTurnInput(BaseModel):
    """Đầu vào đồng nhất cho cả Chat và Voice gửi vào Core."""
    session_id: UUID | str
    turn_index: int
    text_content: str = Field(..., description="Nội dung ứng viên gõ (Chat) hoặc text STT bóc băng (Voice)")
    modality: str = Field(default="CHAT", description="CHAT | VOICE | VIDEO")
    duration_seconds: float = Field(default=0.0, description="Thời gian ứng viên hoàn thành câu trả lời")
    telemetry: Dict[str, Any] = Field(default_factory=dict, description="Metadata: VAD pauses, typing speed, proctoring flags...")


class InterviewerTurnOutput(BaseModel):
    """Đầu ra từ Core gửi về cho ChatAdapter hoặc VoiceAdapter."""
    session_id: UUID | str
    turn_index: int
    message_text: str = Field(..., description="Nội dung AI cần hiển thị trên UI hoặc ném vào TTS để đọc")
    action: TurnAction
    current_stage: InterviewStage
    is_session_finished: bool = False
    current_competency: Optional[str] = None
    time_remaining_seconds: int = 0
    exit_reason: Optional[SessionExitReason] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class QuestionItem(BaseModel):
    """Câu hỏi đã đóng băng từ P2 Question Bank."""
    question_id: str
    question_version_id: Optional[str] = None
    rubric_version_id: Optional[str] = None
    competency: str
    stage: InterviewStage = InterviewStage.DEEP_DIVE
    main_prompt: str
    rubric_criteria: Optional[Any] = ""
    difficulty: str = "intermediate"  # foundational | intermediate | advanced

    model_config = {"extra": "ignore"}


class StageConfig(BaseModel):
    budget_seconds: int
    is_enabled: bool = True
