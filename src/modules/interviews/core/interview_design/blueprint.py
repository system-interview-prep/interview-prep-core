"""Bản thiết kế phỏng vấn: chính sách giai đoạn do chuyên gia cấu hình và lập phiên bản.

Trước khi có mô-đun này, mọi con số trong luồng giai đoạn (thời lượng lượt mặc
định, tỷ lệ phân bổ thời gian, thời gian dự phòng và giới hạn hỏi đáp cuối buổi)
được rải rác dưới dạng hằng số trong bộ lập kế hoạch, bộ chọn câu hỏi và công cụ
điều phối. Giờ đây chúng nằm trong một đối tượng được xác thực duy nhất.
``default_blueprint()`` chứa đúng các giá trị của những hằng số đó, vì vậy một
phiên không có bản thiết kế được cấu hình vẫn hoạt động như trước.

Các *loại* giai đoạn vẫn cố định (WARM_UP, VALIDATE, DEEP_DIVE, CHALLENGE,
BEHAVIORAL, CLOSING): công cụ điều phối, hệ thống chấm điểm và báo cáo đều hiểu
từng loại. Bản thiết kế chỉ điều chỉnh chúng và tắt các giai đoạn tùy chọn; không
thể tạo thêm loại mới. Các phiên demo bỏ qua bản thiết kế
(core.interview_design.demo_mode) để kịch bản demo vẫn giữ nguyên.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

BLUEPRINT_SCHEMA_VERSION = "1.0"
# Các gói thời lượng phỏng vấn đang cung cấp trên giao diện; không tính gói demo.
PACKAGE_MINUTES = (15, 25, 45)


class _Policy(BaseModel):
    """Lớp cơ sở cho các chính sách giai đoạn phỏng vấn.
    - model_config: là cấu hình mô hình Pydantic để bỏ qua các trường không xác định.
    """
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class WarmUpPolicy(_Policy):
    """Chính sách cho giai đoạn chào hỏi và tạo nhịp.
    - soft_answer_seconds: là thời gian mềm cho câu hỏi chào hỏi,
    - hard_answer_seconds: là thời gian cứng cho câu hỏi chào hỏi.
    """
    soft_answer_seconds: int = Field(120, alias="softAnswerSeconds", ge=30, le=600)
    hard_answer_seconds: int = Field(180, alias="hardAnswerSeconds", ge=30, le=900)


class ValidatePolicy(_Policy):
    """Chính sách cho giai đoạn kiểm tra tính trung thực của CV (Fast-fail check).
    - enabled: là trạng thái bật/tắt giai đoạn này,
    - soft_answer_seconds: là thời gian mềm cho câu hỏi kiểm tra,
    - hard_answer_seconds: là thời gian cứng cho câu hỏi kiểm tra,
    - probe_enabled: là trạng thái bật/tắt chức năng thăm dò.
    - until_ratio: là tỷ lệ phần trăm của phiên mà giai đoạn này được phép tiếp tục hỏi."""
    enabled: bool = True
    soft_answer_seconds: int = Field(180, alias="softAnswerSeconds", ge=30, le=900)
    hard_answer_seconds: int = Field(240, alias="hardAnswerSeconds", ge=30, le=1200)
    probe_enabled: bool = Field(True, alias="probeEnabled")
    # Tiếp tục hỏi câu VALIDATE cho đến khi đã dùng hết tỷ lệ thời lượng này.
    until_ratio: float = Field(0.30, alias="untilRatio", gt=0, lt=1)


class TechnicalPolicy(_Policy):
    """Các câu hỏi DEEP_DIVE và CHALLENGE từ Ngân hàng câu hỏi.
    - max_questions: là số lượng câu hỏi tối đa cho giai đoạn kỹ thuật,
    - expected_seconds_per_question: là thời gian dự kiến cho mỗi câu hỏi kỹ thuật,
    - probe_enabled: là trạng thái bật/tắt chức năng thăm dò,
    - deep_dive_until_ratio: là tỷ lệ phần trăm của phiên mà giai đoạn DEEP_DIVE được phép tiếp tục hỏi,
    - challenge_until_ratio: là tỷ lệ phần trăm của phiên mà giai đoạn CHALLENGE được phép tiếp tục hỏi,
    - challenge_for_coding: là trạng thái bật/tắt chức năng thách thức cho các câu hỏi lập trình,
    - challenge_for_cv_gaps: là trạng thái bật/tắt chức năng thách thức cho các khoảng trống trong CV.
    - fast_fail_after_give_ups: là số lần liên tiếp "Tôi bỏ cuộc" để kết thúc phiên sớm.
    """

    max_questions: int = Field(8, alias="maxQuestions", ge=1, le=12)
    # Ngân sách mỗi câu: 30 giây suy nghĩ, 150 giây trả lời và 60 giây hỏi sâu.
    expected_seconds_per_question: int = Field(240, alias="expectedSecondsPerQuestion", ge=60, le=1200)
    probe_enabled: bool = Field(True, alias="probeEnabled")
    deep_dive_until_ratio: float = Field(0.75, alias="deepDiveUntilRatio", gt=0, lt=1)
    challenge_until_ratio: float = Field(0.85, alias="challengeUntilRatio", gt=0, le=1)
    challenge_for_coding: bool = Field(True, alias="challengeForCoding")
    challenge_for_cv_gaps: bool = Field(True, alias="challengeForCvGaps")
    # Kết thúc sớm sau số câu liên tiếp ứng viên chọn bỏ qua này.
    fast_fail_after_give_ups: int = Field(2, alias="fastFailAfterGiveUps", ge=1, le=10)


class BehavioralPolicy(_Policy):
    """Các câu hỏi hành vi.
    - enabled: là trạng thái bật/tắt giai đoạn này,
    - soft_answer_seconds: là thời gian mềm cho câu hỏi hành vi,
    - hard_answer_seconds: là thời gian cứng cho câu hỏi hành vi,
    - reserve_seconds: là thời gian được dành riêng cho giai đoạn này."""
    enabled: bool = True
    soft_answer_seconds: int = Field(180, alias="softAnswerSeconds", ge=30, le=900)
    hard_answer_seconds: int = Field(240, alias="hardAnswerSeconds", ge=30, le=1200)
    # Thời gian giữ lại để hỏi câu hành vi sau phần kỹ thuật.
    reserve_seconds: int = Field(180, alias="reserveSeconds", ge=0, le=900)


class ClosingPolicy(_Policy):
    """Câu hỏi của ứng viên và lời chào kết thúc.
    - qna_enabled: là trạng thái bật/tắt chức năng hỏi đáp cuối buổi,
    - max_questions: là số lượng câu hỏi tối đa cho giai đoạn này,
    - budget_seconds: là thời gian ngân sách cho giai đoạn này,
    - reserve_seconds: là thời gian được dành riêng cho giai đoạn này.
    - min_seconds_to_open: là thời gian tối thiểu còn lại để mở phần hỏi đáp.
    """
    qna_enabled: bool = Field(True, alias="qnaEnabled")
    max_questions: int = Field(2, alias="maxQuestions", ge=1, le=5)
    budget_seconds: int = Field(180, alias="budgetSeconds", ge=30, le=900)
    reserve_seconds: int = Field(60, alias="reserveSeconds", ge=0, le=600)
    # Chỉ mở phần hỏi đáp khi thời gian còn lại ít nhất bằng mức này.
    min_seconds_to_open: int = Field(180, alias="minSecondsToOpen", ge=0, le=900)


class PacingPolicy(_Policy):
    """Quy định cách điều phối câu hỏi khi buổi phỏng vấn gần hết giờ.
    - emergency_cutoff_seconds: là thời gian còn lại tối thiểu để bắt đầu câu hỏi mới,
    - behind_schedule_ratio: là tỷ lệ thời lượng mà câu VALIDATE/DEEP_DIVE sẽ không được hỏi sâu.
    """
    # Không bắt đầu câu hỏi mới khi thời gian còn lại thấp hơn mức này.
    emergency_cutoff_seconds: int = Field(90, alias="emergencyCutoffSeconds", ge=0, le=600)
    # Qua tỷ lệ thời lượng này, câu VALIDATE/DEEP_DIVE sẽ không được hỏi sâu.
    behind_schedule_ratio: float = Field(0.8, alias="behindScheduleRatio", gt=0, le=1)


class InterviewBlueprint(_Policy):
    """Bản thiết kế phỏng vấn: chính sách giai đoạn do chuyên gia cấu hình và lập phiên bản.
    - schema_version: là phiên bản của bản thiết kế,
    - warm_up: là chính sách cho giai đoạn chào hỏi và tạo nhịp,
    - validate_cv: là chính sách cho giai đoạn kiểm tra tính trung thực của CV
    - technical: là chính sách cho các câu hỏi DEEP_DIVE và CHALLENGE từ Ngân hàng câu hỏi,
    - behavioral: là chính sách cho các câu hỏi hành vi,
    - closing: là chính sách cho giai đoạn kết thúc buổi phỏng vấn,
    - pacing: là chính sách điều phối thời gian trong buổi phỏng vấn.
    
    + preset_expected_seconds(): tính thời gian dự kiến cho các phần có trong mọi buổi và phần dự phòng cuối buổi,
    + technical_question_budget(duration_minutes): tính số lượng câu hỏi kỹ thuật dự kiến cho thời lượng phỏng vấn đã cho,
    + preview(): mô phỏng số câu và tổng thời gian cho từng gói trước khi kích hoạt cấu hình,
    + activation_errors(): trả về lý do cấu hình không thể kích hoạt; danh sách rỗng nếu hợp lệ,
    + to_payload(): chuyển cấu hình sang dữ liệu JSON để lưu hoặc truyền qua API.
    """
    schema_version: str = Field(BLUEPRINT_SCHEMA_VERSION, alias="schemaVersion")
    warm_up: WarmUpPolicy = Field(default_factory=WarmUpPolicy, alias="warmUp")
    validate_cv: ValidatePolicy = Field(default_factory=ValidatePolicy, alias="validate")
    technical: TechnicalPolicy = Field(default_factory=TechnicalPolicy)
    behavioral: BehavioralPolicy = Field(default_factory=BehavioralPolicy)
    closing: ClosingPolicy = Field(default_factory=ClosingPolicy)
    pacing: PacingPolicy = Field(default_factory=PacingPolicy)

    @model_validator(mode="after")
    def _consistent(self) -> InterviewBlueprint:
        for name, policy in (
            ("warmUp", self.warm_up),
            ("validate", self.validate_cv),
            ("behavioral", self.behavioral),
        ):
            if policy.hard_answer_seconds < policy.soft_answer_seconds:
                raise ValueError(f"{name}: hardAnswerSeconds must be >= softAnswerSeconds")
        if not (
            self.validate_cv.until_ratio
            < self.technical.deep_dive_until_ratio
            <= self.technical.challenge_until_ratio
        ):
            raise ValueError(
                "Ratios must satisfy validate.untilRatio < deepDiveUntilRatio <= challengeUntilRatio"
            )
        return self

    def preset_expected_seconds(self) -> int:
        """Tính thời gian dự kiến cho các phần có trong mọi buổi và phần dự phòng cuối buổi."""
        seconds = self.warm_up.soft_answer_seconds + self.closing.reserve_seconds
        if self.validate_cv.enabled:
            seconds += self.validate_cv.soft_answer_seconds
        if self.behavioral.enabled:
            seconds += self.behavioral.soft_answer_seconds
        return seconds

    def technical_question_budget(self, duration_minutes: int) -> int:
        available = int(duration_minutes) * 60 - self.preset_expected_seconds()
        per_question = self.technical.expected_seconds_per_question
        return max(1, min(self.technical.max_questions, available // per_question))

    def preview(self) -> list[dict[str, Any]]:
        """Mô phỏng số câu và tổng thời gian cho từng gói trước khi kích hoạt cấu hình."""
        rows = []
        for minutes in PACKAGE_MINUTES:
            questions = self.technical_question_budget(minutes)
            expected = (
                self.preset_expected_seconds() + questions * self.technical.expected_seconds_per_question
            )
            rows.append(
                {
                    "durationMinutes": minutes,
                    "technicalQuestions": questions,
                    "expectedSeconds": expected,
                    "fits": expected <= minutes * 60,
                }
            )
        return rows

    def activation_errors(self) -> list[str]:
        """Trả về lý do cấu hình không thể kích hoạt; danh sách rỗng nếu hợp lệ."""
        return [
            f"Gói {row['durationMinutes']} phút cần khoảng {row['expectedSeconds'] // 60} phút, "
            "vượt thời lượng."
            for row in self.preview()
            if not row["fits"]
        ]

    def to_payload(self) -> dict[str, Any]:
        """Chuyển cấu hình sang dữ liệu JSON để lưu hoặc truyền qua API."""
        return self.model_dump(by_alias=True, mode="json")


def default_blueprint() -> InterviewBlueprint:
    return InterviewBlueprint()


def blueprint_from_payload(payload: Any) -> InterviewBlueprint:
    """Đọc cấu hình đã lưu trong buổi; dùng mặc định nếu thiếu hoặc dữ liệu không hợp lệ."""
    if isinstance(payload, InterviewBlueprint):
        return payload
    if isinstance(payload, dict) and payload:
        try:
            return InterviewBlueprint.model_validate(payload)
        except ValueError:
            return default_blueprint()
    return default_blueprint()
