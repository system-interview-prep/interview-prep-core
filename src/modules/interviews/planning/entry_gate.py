"""Điều kiện vào phòng phỏng vấn và cách kết quả matching điều chỉnh buổi.

Mặc định là **chế độ luyện tập**: chỉ chặn khi không chấm được CV với JD (CV hoặc
JD chưa phân tích xong, matching lỗi), vì khi đó không có gì để lập kế hoạch.
Các trường hợp khác vẫn vào được, kèm cảnh báo, và buổi được điều chỉnh:

* trượt yêu cầu bắt buộc (``fit_band == "not_eligible"``): cảnh báo; kế hoạch vẫn
  bắt buộc có mục tiêu đánh giá cho mọi yêu cầu bắt buộc
  (``validate_must_have_coverage``) và ưu tiên yêu cầu chưa đạt (×1,35);
* điểm dưới mức "phù hợp thấp" (mặc định 40%): cảnh báo, ứng viên xác nhận trên
  giao diện; độ khó của buổi hạ một bậc so với JD;
* không có điểm số: lời nhắc bổ sung CV.

**Mức luyện theo điểm** (điểm là phần thưởng, không phải cửa chặn), để ứng viên
muốn sửa CV cho điểm cao hơn:

| Mức | Khi nào | Buổi phỏng vấn |
|---|---|---|
| ``basic`` (cơ bản) | dưới 40% hoặc trượt yêu cầu bắt buộc | dễ hơn JD một bậc |
| ``standard`` (đúng mức vị trí) | 40–69%, hoặc chưa có điểm | đúng độ khó của JD |
| ``challenge`` (thử thách) | từ 70% và đủ điều kiện | khó hơn JD một bậc, và hỏi đào sâu cả khi câu trả lời đã đủ ý |

``INTERVIEW_CHALLENGE_PERCENT`` (mặc định 70) chỉnh mức mở khoá thử thách.

Lý do (phân tích ngày 19/10, `DOC_AND_PLAN/plan/interview/INTERVIEW_ENTRY_THRESHOLD_2026-10-19.md`):
trên bộ đo ``score_calibration_v1`` (60 cặp), ngưỡng cứng 40% cộng hai điều kiện
kia chặn khoảng 54/60 cặp, trong khi điểm chuẩn chỉ có 3/60 cặp dưới 40%. Điểm của
hệ thống đang lệch thấp, nên chặn theo điểm sẽ chặn oan, và chặn người điểm thấp
đi ngược mục tiêu luyện tập.

**Chế độ sàng lọc** (bật bằng biến môi trường, chỉ nên dùng khi điểm đã được hiệu
chỉnh lại) chặn hẳn các trường hợp trên:

* ``INTERVIEW_MIN_MATCH_PERCENT`` (mặc định 0 = không chặn theo điểm)
* ``INTERVIEW_BLOCK_NOT_ELIGIBLE`` (mặc định 0)
* ``INTERVIEW_REQUIRE_MATCH_SCORE`` (mặc định 0)
* ``INTERVIEW_LOW_FIT_PERCENT`` (mặc định 40): chỉ để cảnh báo và hạ độ khó.

Điểm dùng để so là đúng điểm giao diện hiển thị (``normalizeMatchResult`` ở
frontend). Trang báo cáo và kế hoạch dùng chung bộ nhớ đệm matching.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Literal

from src.modules.matching.schemas import MatchResult

DEFAULT_MIN_MATCH_PERCENT = 0
DEFAULT_LOW_FIT_PERCENT = 40
DEFAULT_CHALLENGE_PERCENT = 70

PracticeTier = Literal["basic", "standard", "challenge"]

EntryWarning = Literal["match_not_eligible", "match_low_fit", "match_score_unavailable"]

EntryBlockReason = Literal["match_score_unavailable", "match_not_eligible", "match_below_threshold"]

_MESSAGES_VI: dict[str, str] = {
    "match_score_unavailable": (
        "Chưa có điểm phù hợp giữa CV và vị trí này nên chưa thể vào phòng phỏng vấn. "
        "Bạn bổ sung thông tin trong CV rồi chấm lại nhé."
    ),
    "match_not_eligible": (
        "CV chưa đạt yêu cầu bắt buộc của vị trí này nên chưa thể vào phòng phỏng vấn. "
        "Bạn xem các yêu cầu còn thiếu trong báo cáo phù hợp."
    ),
    "match_below_threshold": (
        "Điểm phù hợp {percentage}% thấp hơn mức tối thiểu {min_percent}% để vào phòng phỏng vấn."
    ),
}


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "off", "no"}


def _percent(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(float(raw)) if raw else default
    except ValueError:
        value = default
    return max(0, min(100, value))


@dataclass(frozen=True)
class EntryPolicy:
    min_match_percent: int
    block_not_eligible: bool
    require_match_score: bool
    low_fit_percent: int = DEFAULT_LOW_FIT_PERCENT
    challenge_percent: int = DEFAULT_CHALLENGE_PERCENT

    def to_payload(self) -> dict[str, Any]:
        return {
            "minMatchPercent": self.min_match_percent,
            "blockNotEligible": self.block_not_eligible,
            "requireMatchScore": self.require_match_score,
            "lowFitPercent": self.low_fit_percent,
            "challengePercent": self.challenge_percent,
        }


def entry_policy() -> EntryPolicy:
    return EntryPolicy(
        min_match_percent=_percent("INTERVIEW_MIN_MATCH_PERCENT", DEFAULT_MIN_MATCH_PERCENT),
        block_not_eligible=_flag("INTERVIEW_BLOCK_NOT_ELIGIBLE", False),
        require_match_score=_flag("INTERVIEW_REQUIRE_MATCH_SCORE", False),
        low_fit_percent=_percent("INTERVIEW_LOW_FIT_PERCENT", DEFAULT_LOW_FIT_PERCENT),
        challenge_percent=_percent("INTERVIEW_CHALLENGE_PERCENT", DEFAULT_CHALLENGE_PERCENT),
    )


def match_display_percentage(match: MatchResult) -> int | None:
    """The percentage the candidate sees on the fit report (0-100), or None."""
    suitability = (
        match.suitability_score
        if match.decision == "assessed"
        and match.eligibility == "eligible"
        and match.fit_band in {"strong_fit", "partial_fit"}
        else None
    )
    value = suitability if suitability is not None else match.diagnostic_score
    if value is None:
        return None
    return max(0, min(100, round(float(value) * 100)))


@dataclass(frozen=True)
class EntryDecision:
    allowed: bool
    reason: EntryBlockReason | None
    percentage: int | None
    min_percent: int
    fit_band: str
    eligibility: str
    warnings: tuple[EntryWarning, ...] = ()
    tier: PracticeTier = "standard"

    def detail(self) -> dict[str, Any]:
        message = _MESSAGES_VI.get(self.reason or "", "")
        return {
            "errorCode": self.reason,
            "message": message.format(percentage=self.percentage, min_percent=self.min_percent),
            "details": {
                "percentage": self.percentage,
                "minMatchPercent": self.min_percent,
                "fitBand": self.fit_band,
                "eligibility": self.eligibility,
            },
        }

    def trace_fields(self) -> dict[str, Any]:
        fields = {f"gate_{key}": value for key, value in asdict(self).items()}
        fields["gate_warnings"] = list(self.warnings)
        return fields


def evaluate_entry(match: MatchResult, policy: EntryPolicy | None = None) -> EntryDecision:
    policy = policy or entry_policy()
    percentage = match_display_percentage(match)
    not_eligible = match.fit_band == "not_eligible" or match.eligibility == "ineligible"
    warnings: list[EntryWarning] = []
    if not_eligible:
        warnings.append("match_not_eligible")
    if percentage is None:
        warnings.append("match_score_unavailable")
    elif percentage < policy.low_fit_percent:
        warnings.append("match_low_fit")
    if "match_low_fit" in warnings or "match_not_eligible" in warnings:
        tier: PracticeTier = "basic"
    elif (
        percentage is not None
        and percentage >= policy.challenge_percent
        # "Đủ điều kiện": the final suitability score, not a diagnostic one.
        and match.eligibility == "eligible"
        and match.decision == "assessed"
    ):
        tier = "challenge"
    else:
        tier = "standard"

    def decide(reason: EntryBlockReason | None) -> EntryDecision:
        return EntryDecision(
            allowed=reason is None,
            reason=reason,
            percentage=percentage,
            min_percent=policy.min_match_percent,
            fit_band=match.fit_band,
            eligibility=match.eligibility,
            warnings=tuple(warnings),
            tier=tier,
        )

    if policy.block_not_eligible and not_eligible:
        return decide("match_not_eligible")
    if percentage is None:
        return decide("match_score_unavailable" if policy.require_match_score else None)
    if percentage < policy.min_match_percent:
        return decide("match_below_threshold")
    return decide(None)


_LEVELS = ("foundational", "intermediate", "advanced")


def fit_adjusted_difficulty(difficulty: dict[str, Any], decision: EntryDecision) -> dict[str, Any]:
    """Độ khó của buổi theo mức luyện: cơ bản hạ một bậc, thử thách tăng một bậc.

    Tối đa một bậc mỗi chiều, trong khoảng foundational–advanced. Độ khó của JD
    được giữ trong ``jobLevel`` để báo cáo và lần luyện sau so được. Mức luyện
    luôn được ghi vào ``practiceTier``, kể cả khi độ khó không đổi.
    """
    level = str(difficulty.get("level") or "")
    result = {**difficulty, "practiceTier": decision.tier}
    if level not in _LEVELS:
        return result
    index = _LEVELS.index(level)
    if decision.tier == "basic" and index > 0:
        reasons = sorted({"match_low_fit", "match_not_eligible"} & set(decision.warnings))
        new_index, direction = index - 1, "easier"
    elif decision.tier == "challenge" and index < len(_LEVELS) - 1:
        reasons = ["match_challenge_unlocked"]
        new_index, direction = index + 1, "harder"
    else:
        return result
    return {
        **result,
        "level": _LEVELS[new_index],
        "jobLevel": level,
        "fitAdjustment": {
            "direction": direction,
            "reasons": reasons,
            "matchPercent": decision.percentage,
        },
    }


class InterviewEntryBlocked(Exception):
    """The CV–job match does not meet the interview entry threshold."""

    def __init__(self, decision: EntryDecision):
        super().__init__(decision.reason or "blocked")
        self.decision = decision
