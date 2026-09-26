# src/modules/interviews/evaluation/evaluation_types.py
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID
from pydantic import BaseModel, Field


class DecisionRecommendation(str, Enum):
    STRONG_PASS = "STRONG_PASS"
    PASS = "PASS"
    CONSIDER = "CONSIDER"
    REJECT = "REJECT"


class StarAnalysis(BaseModel):
    """Bóc tách 4 thành phần theo khung STAR (Situation, Task, Action, Result)."""
    situation: str = Field(default="", description="Bối cảnh vấn đề")
    task: str = Field(default="", description="Nhiệm vụ, mục tiêu cần giải quyết")
    action: str = Field(default="", description="Hành động cụ thể ứng viên đã thực hiện")
    result: str = Field(default="", description="Kết quả đạt được (định lượng/định tính)")
    is_star_complete: bool = Field(default=False, description="Đạt đầy đủ cả 4 cấu phần STAR hay không")


class TurnEvaluationInput(BaseModel):
    """Dữ liệu đầu vào để đánh giá 1 turn câu hỏi."""
    turn_id: str
    turn_index: int
    competency: str = Field(default="Chuyên môn")
    question_prompt: str
    rubric_criteria: Any = Field(default="")
    candidate_answer: str
    weight: float = Field(default=1.0)


class TurnEvaluationResult(BaseModel):
    """Kết quả đánh giá chi tiết cho 1 turn câu hỏi."""
    turn_id: str
    score: float = Field(ge=0.0, le=10.0, description="Thang điểm từ 0.0 đến 10.0")
    star_analysis: StarAnalysis = Field(default_factory=StarAnalysis)
    evidence_quotes: List[str] = Field(default_factory=list, description="Trích dẫn nguyên văn câu nói làm bằng chứng")
    feedback: str = Field(default="", description="Nhận xét chi tiết cho lượt trả lời này")
    strengths: List[str] = Field(default_factory=list, description="Các điểm mạnh được ghi nhận")
    weaknesses: List[str] = Field(default_factory=list, description="Các điểm hạn chế cần cải thiện")


class CompetencyScore(BaseModel):
    """Điểm kỹ năng tổng hợp để vẽ Radar Chart trên Frontend."""
    competency: str
    score: float = Field(ge=0.0, le=10.0)
    weight: float = Field(default=1.0)


class SessionEvaluationResult(BaseModel):
    """Kết quả đánh giá tổng thể toàn bộ buổi phỏng vấn."""
    session_id: str
    overall_score: float = Field(ge=0.0, le=10.0)
    decision_recommendation: DecisionRecommendation
    competency_scores: List[CompetencyScore] = Field(default_factory=list)
    turn_evaluations: List[TurnEvaluationResult] = Field(default_factory=list)
    recruiter_summary: str = Field(..., description="Báo cáo tóm tắt dành cho Nhà tuyển dụng / Hội đồng phỏng vấn")
    candidate_feedback: str = Field(..., description="Báo cáo phản hồi mang tính định hướng phát triển (Coaching) cho Ứng viên")
    next_round_topics: List[str] = Field(default_factory=list, description="Gợi ý các chủ đề cần đào sâu ở vòng tiếp theo")
    red_flags: List[str] = Field(default_factory=list, description="Các cảnh báo nghi vấn (nếu có)")
    evaluated_at: Optional[datetime] = None
