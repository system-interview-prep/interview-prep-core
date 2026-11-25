"""Policy configuration and validation contract for P1 Planner (Gate 2).

This module defines the explicit policy configuration inputs for time envelope
allocation and helper utilities for resolving rule precedence. It deliberately
does not contain defaults for unapproved Product decisions or budget parameters.
"""

from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator

# Allowed enum values for Decision 5 (Coding Fallback) in Gate 2.
# 5C (Target Consolidation) is out-of-scope for Gate 2 and strictly excluded.
CodingFallbackPolicy = Literal["downgrade_to_text", "omit"]


class PlannerPolicyConfig(BaseModel):
    """Explicit configuration for P1 Planner time allocation and branching policies.

    All parameters affecting T_tech_pool and archetype envelopes MUST be explicitly
    provided by callers/test fixtures. There are NO implicit defaults.
    """

    # --- Decision 1: Onboarding structure & reserves (No defaults) ---
    t_onboarding_base: int = Field(
        ...,
        ge=0,
        description="Thời lượng mở đầu cơ sở (giây): 90-120s cho Mode 1 lượt, 210-270s cho Mode 2 lượt",
    )
    n_onboarding: int = Field(
        ...,
        ge=1,
        le=2,
        description="Số lượt mở đầu cố định (1 hoặc 2 lượt)",
    )
    t_cv_addon_inclusive: int = Field(
        ...,
        ge=0,
        description="Dự phòng CV follow-up theo Cách 1 Inclusive (giây)",
    )
    t_cv_standalone_reserve: int = Field(
        ...,
        ge=0,
        description="Quỹ riêng CV follow-up theo Cách 2 Decoupled 1C.2b (giây)",
    )

    # --- Macro Session Reserves (No defaults) ---
    t_behavioral: int = Field(
        ...,
        ge=0,
        description="Ngân sách câu hỏi hành vi STAR (giây)",
    )
    probe_pool_ratio: float = Field(
        ...,
        ge=0.15,
        le=0.20,
        description="Tỷ lệ quỹ đào sâu probe kỹ thuật so với tổng thời lượng phiên (15% - 20% theo đặc tả hiện hành)",
    )
    t_closing_reserve: int = Field(
        ...,
        ge=0,
        description="Ngân sách kết thúc phiên (giây)",
    )

    # --- Envelope Archetype Bounds (No defaults, strictly >= minimum floor guaranteed) ---
    t_arch_text_min: int = Field(
        ...,
        ge=180,
        description="Mức sàn TEXT cơ sở, tối thiểu 180s theo AC-P1-UNIT-03",
    )
    t_arch_text_max: int = Field(
        ...,
        ge=180,
        description="Trần ước tính 1 câu TEXT (phải >= t_arch_text_min >= 180s)",
    )
    t_arch_code_min: int = Field(
        ...,
        ge=360,
        description="Mức sàn CODING cơ sở, tối thiểu 360s theo AC-P1-UNIT-03",
    )
    t_arch_code_max: int = Field(
        ...,
        ge=360,
        description="Trần ước tính 1 bài CODING (phải >= t_arch_code_min >= 360s)",
    )

    # --- Decision 5: Coding Fallback Policy (No default, strictly 5A or 5B) ---
    coding_fallback_policy: CodingFallbackPolicy = Field(
        ...,
        description="Chính sách khi target coding nhận envelope < 360s: 'downgrade_to_text' (5A) hoặc 'omit' (5B)",
    )

    # --- Decision 7: Priority Policy (No default, strictly 7A or 7B) ---
    strict_priority_stop: bool = Field(
        ...,
        description="Chính sách khi target đầu bảng không đủ sàn: True (7A Strict Stop) hoặc False (7B Skip)",
    )

    # --- Live Hands-on Assessment Flag ---
    strict_hands_on_required: bool | None = Field(
        default=None,
        description="Cờ yêu cầu bắt buộc live coding hands-on. None nghĩa là cấu hình không áp đặt.",
    )

    @model_validator(mode="after")
    def validate_durations_and_non_double_counting(self) -> "PlannerPolicyConfig":
        # 1. Đồng bộ miền t_onboarding_base theo mode với đặc tả:
        #    Mode 1: 90–120 giây; Mode 2: 210–270 giây.
        if self.n_onboarding == 1:
            if not (90 <= self.t_onboarding_base <= 120):
                raise ValueError(
                    f"Mode 1 turn (n_onboarding=1) requires t_onboarding_base between 90s and 120s, "
                    f"got {self.t_onboarding_base}s"
                )
        elif self.n_onboarding == 2:
            if not (210 <= self.t_onboarding_base <= 270):
                raise ValueError(
                    f"Mode 2 turns (n_onboarding=2) requires t_onboarding_base between 210s and 270s, "
                    f"got {self.t_onboarding_base}s"
                )

        # 2. Bounds checks for Archetype durations
        if self.t_arch_text_min > self.t_arch_text_max:
            raise ValueError(
                f"t_arch_text_min ({self.t_arch_text_min}) cannot exceed t_arch_text_max ({self.t_arch_text_max})"
            )
        if self.t_arch_code_min > self.t_arch_code_max:
            raise ValueError(
                f"t_arch_code_min ({self.t_arch_code_min}) cannot exceed t_arch_code_max ({self.t_arch_code_max})"
            )

        # 3. Strict protection against double counting CV follow-up reserve
        # (Cannot apply both Inclusive add-on and Decoupled standalone reserve simultaneously)
        if self.t_cv_addon_inclusive > 0 and self.t_cv_standalone_reserve > 0:
            raise ValueError(
                "Conflict in CV follow-up reserve: cannot set both t_cv_addon_inclusive > 0 "
                "and t_cv_standalone_reserve > 0 simultaneously (double-counting reserve violation)"
            )

        return self


def resolve_strict_hands_on_precedence(
    *,
    job: Any,
    config: PlannerPolicyConfig | None,
) -> bool | None:
    """Resolve the precedence of `strict_hands_on_required` with fail-safe priority.

    Rules:
    1. A mandatory flag on the Job cannot be weakened by config:
       If `job.strict_hands_on_required == True`, the result is ALWAYS True,
       even if config specifies False.
    2. Conflict resolution: If Job is False but Config is True, the stricter
       fail-safe requirement wins (True).
    3. If Job is False and Config is False, result is False.
    4. If Job is False and Config is None, result is False (Job explicitly disabled).
    5. If Job has no flag (None):
       - If Config has a value, use Config's value.
       - If Config is also None (both missing), return None.
       CRITICAL: When both are missing, DO NOT assume False.
    """
    job_flag: bool | None = getattr(job, "strict_hands_on_required", None)
    config_flag: bool | None = config.strict_hands_on_required if config is not None else None

    # Job mandatory requirement cannot be weakened
    if job_flag is True:
        return True

    # Job is explicitly False
    if job_flag is False:
        if config_flag is True:
            # Config demands stricter assessment -> safer option wins
            return True
        return False

    # Job flag is None / missing
    if config_flag is not None:
        return config_flag

    # Both missing
    return None
