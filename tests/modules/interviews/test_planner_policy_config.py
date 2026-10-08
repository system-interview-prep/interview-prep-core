"""Unit tests for P1 Planner Policy Configuration Contract and Precedence (Gate 2 Task 1).

Covers input validation, double-counting reserve protection, absence of implicit
defaults for unapproved Product decisions, strict domain bounds (probe_pool_ratio 15-20%,
t_onboarding_base by mode), and precedence resolution for `strict_hands_on_required`.
"""

from typing import Any
import pytest
from pydantic import ValidationError

from src.modules.interviews.planning.planner_config import (
    PlannerPolicyConfig,
    resolve_strict_hands_on_precedence,
)


def create_test_policy_config(
    *,
    # NOTE: These values are explicit analytical test parameters only.
    # They DO NOT represent approved Product defaults.
    t_onboarding_base: int = 120,
    n_onboarding: int = 1,
    t_cv_addon_inclusive: int = 0,
    t_cv_standalone_reserve: int = 0,
    t_behavioral: int = 210,
    probe_pool_ratio: float = 0.15,
    t_closing_reserve: int = 180,
    t_arch_text_min: int = 180,
    t_arch_text_max: int = 240,
    t_arch_code_min: int = 360,
    t_arch_code_max: int = 480,
    coding_fallback_policy: str = "downgrade_to_text",
    strict_priority_stop: bool = True,
    strict_hands_on_required: bool | None = None,
) -> PlannerPolicyConfig:
    """Explicit test factory for creating test policy configurations.

    Warning: Product decisions (Decision 1, 5, 7) and budget parameters must
    remain explicit in test fixtures.
    """
    return PlannerPolicyConfig(
        t_onboarding_base=t_onboarding_base,
        n_onboarding=n_onboarding,
        t_cv_addon_inclusive=t_cv_addon_inclusive,
        t_cv_standalone_reserve=t_cv_standalone_reserve,
        t_behavioral=t_behavioral,
        probe_pool_ratio=probe_pool_ratio,
        t_closing_reserve=t_closing_reserve,
        t_arch_text_min=t_arch_text_min,
        t_arch_text_max=t_arch_text_max,
        t_arch_code_min=t_arch_code_min,
        t_arch_code_max=t_arch_code_max,
        coding_fallback_policy=coding_fallback_policy,  # type: ignore[arg-type]
        strict_priority_stop=strict_priority_stop,
        strict_hands_on_required=strict_hands_on_required,
    )


class _DummyJob:
    """Mock job object for testing precedence resolution."""

    def __init__(self, strict_hands_on_required: Any = None) -> None:
        if strict_hands_on_required is not None or strict_hands_on_required is False:
            self.strict_hands_on_required = strict_hands_on_required


# --- Test Group 1: Absence of Implicit Defaults for Budget Parameters & Open Decisions ---


def test_config_requires_all_budget_and_decision_fields_explicitly() -> None:
    """PlannerPolicyConfig must fail if caller does not explicitly provide budget and decisions."""
    with pytest.raises(ValidationError) as exc_info:
        PlannerPolicyConfig()  # type: ignore[call-arg]

    errors = exc_info.value.errors()
    missing_fields = {error["loc"][0] for error in errors if error["type"] == "missing"}

    # Must require explicit inputs for all budget parameters affecting T_tech_pool or archetypes
    expected_required_fields = {
        "t_onboarding_base",
        "n_onboarding",
        "t_cv_addon_inclusive",
        "t_cv_standalone_reserve",
        "t_behavioral",
        "probe_pool_ratio",
        "t_closing_reserve",
        "t_arch_text_min",
        "t_arch_text_max",
        "t_arch_code_min",
        "t_arch_code_max",
        "coding_fallback_policy",
        "strict_priority_stop",
    }
    assert expected_required_fields.issubset(missing_fields)


# --- Test Group 2: Mode-Specific t_onboarding_base Validation (Mode 1: 90-120s, Mode 2: 210-270s) ---


def test_config_validates_mode_1_onboarding_base_domain() -> None:
    # Mode 1 valid bounds [90, 120]
    cfg_lower = create_test_policy_config(n_onboarding=1, t_onboarding_base=90)
    assert cfg_lower.t_onboarding_base == 90

    cfg_upper = create_test_policy_config(n_onboarding=1, t_onboarding_base=120)
    assert cfg_upper.t_onboarding_base == 120

    # Below 90s fails
    with pytest.raises(ValidationError) as exc_info:
        create_test_policy_config(n_onboarding=1, t_onboarding_base=89)
    assert "Mode 1 turn (n_onboarding=1) requires t_onboarding_base between 90s and 120s" in str(exc_info.value)

    # Above 120s fails
    with pytest.raises(ValidationError) as exc_info:
        create_test_policy_config(n_onboarding=1, t_onboarding_base=121)
    assert "Mode 1 turn (n_onboarding=1) requires t_onboarding_base between 90s and 120s" in str(exc_info.value)


def test_config_validates_mode_2_onboarding_base_domain() -> None:
    # Mode 2 valid bounds [210, 270]
    cfg_lower = create_test_policy_config(n_onboarding=2, t_onboarding_base=210)
    assert cfg_lower.t_onboarding_base == 210

    cfg_upper = create_test_policy_config(n_onboarding=2, t_onboarding_base=270)
    assert cfg_upper.t_onboarding_base == 270

    # Below 210s fails
    with pytest.raises(ValidationError) as exc_info:
        create_test_policy_config(n_onboarding=2, t_onboarding_base=209)
    assert "Mode 2 turns (n_onboarding=2) requires t_onboarding_base between 210s and 270s" in str(exc_info.value)

    # Above 270s fails
    with pytest.raises(ValidationError) as exc_info:
        create_test_policy_config(n_onboarding=2, t_onboarding_base=271)
    assert "Mode 2 turns (n_onboarding=2) requires t_onboarding_base between 210s and 270s" in str(exc_info.value)


# --- Test Group 3: probe_pool_ratio Validation (Strict 15%-20%) ---


def test_config_validates_probe_pool_ratio_strictly_15_to_20_percent() -> None:
    # Valid lower and upper bounds
    cfg_15 = create_test_policy_config(probe_pool_ratio=0.15)
    assert cfg_15.probe_pool_ratio == 0.15

    cfg_20 = create_test_policy_config(probe_pool_ratio=0.20)
    assert cfg_20.probe_pool_ratio == 0.20

    # Below 0.15 (15%) is rejected
    with pytest.raises(ValidationError):
        create_test_policy_config(probe_pool_ratio=0.14)

    # Above 0.20 (20%) is rejected
    with pytest.raises(ValidationError):
        create_test_policy_config(probe_pool_ratio=0.21)


# --- Test Group 4: Bounds & Policies Validation ---


def test_config_accepts_exact_minimum_floor_bounds() -> None:
    """Config must accept floor bounds at exact minimums (180s for TEXT, 360s for CODING)."""
    cfg = create_test_policy_config(
        t_arch_text_min=180,
        t_arch_text_max=240,
        t_arch_code_min=360,
        t_arch_code_max=480,
    )
    assert cfg.t_arch_text_min == 180
    assert cfg.t_arch_text_max == 240
    assert cfg.t_arch_code_min == 360
    assert cfg.t_arch_code_max == 480


def test_config_rejects_sub_minimum_archetype_floors() -> None:
    """Config must reject floor bounds below 180s for TEXT and below 360s for CODING."""
    # TEXT min below 180s (e.g. 179s)
    with pytest.raises(ValidationError):
        create_test_policy_config(t_arch_text_min=179)

    # TEXT max below 180s (e.g. 179s)
    with pytest.raises(ValidationError):
        create_test_policy_config(t_arch_text_min=180, t_arch_text_max=179)

    # CODING min below 360s (e.g. 359s)
    with pytest.raises(ValidationError):
        create_test_policy_config(t_arch_code_min=359)

    # CODING max below 360s (e.g. 359s)
    with pytest.raises(ValidationError):
        create_test_policy_config(t_arch_code_min=360, t_arch_code_max=359)


def test_config_rejects_inverted_envelope_bounds() -> None:
    # Inverted TEXT bounds (min > max)
    with pytest.raises(ValidationError) as exc_info:
        create_test_policy_config(t_arch_text_min=250, t_arch_text_max=240)
    assert "t_arch_text_min (250) cannot exceed t_arch_text_max (240)" in str(exc_info.value)

    # Inverted CODING bounds (min > max)
    with pytest.raises(ValidationError) as exc_info:
        create_test_policy_config(t_arch_code_min=500, t_arch_code_max=400)
    assert "t_arch_code_min (500) cannot exceed t_arch_code_max (400)" in str(exc_info.value)


def test_config_rejects_unsupported_coding_fallback_policies() -> None:
    # Valid 5A and 5B
    cfg_5a = create_test_policy_config(coding_fallback_policy="downgrade_to_text")
    assert cfg_5a.coding_fallback_policy == "downgrade_to_text"

    cfg_5b = create_test_policy_config(coding_fallback_policy="omit")
    assert cfg_5b.coding_fallback_policy == "omit"

    # Reject 5C (Out of Scope for Gate 2)
    with pytest.raises(ValidationError):
        create_test_policy_config(coding_fallback_policy="5C")

    with pytest.raises(ValidationError):
        create_test_policy_config(coding_fallback_policy="target_consolidation")


def test_config_rejects_double_counting_cv_reserve() -> None:
    # Setting both Inclusive add-on and Decoupled standalone reserve must fail
    with pytest.raises(ValidationError) as exc_info:
        create_test_policy_config(
            t_cv_addon_inclusive=90,
            t_cv_standalone_reserve=90,
        )
    assert "Conflict in CV follow-up reserve: cannot set both" in str(exc_info.value)


# --- Test Group 5: Precedence Resolution for strict_hands_on_required ---


def test_precedence_job_true_cannot_be_weakened_by_config() -> None:
    # Job=True, Config=False -> True wins (fail-safe)
    job = _DummyJob(strict_hands_on_required=True)
    cfg = create_test_policy_config(strict_hands_on_required=False)
    assert resolve_strict_hands_on_precedence(job=job, config=cfg) is True

    # Job=True, Config=None -> True
    cfg_none = create_test_policy_config(strict_hands_on_required=None)
    assert resolve_strict_hands_on_precedence(job=job, config=cfg_none) is True


def test_precedence_conflict_resolution_safer_option_wins() -> None:
    # Job=False, Config=True -> True wins (config demands stricter verification)
    job = _DummyJob(strict_hands_on_required=False)
    cfg = create_test_policy_config(strict_hands_on_required=True)
    assert resolve_strict_hands_on_precedence(job=job, config=cfg) is True


def test_precedence_both_false() -> None:
    # Job=False, Config=False -> False
    job = _DummyJob(strict_hands_on_required=False)
    cfg = create_test_policy_config(strict_hands_on_required=False)
    assert resolve_strict_hands_on_precedence(job=job, config=cfg) is False

    # Job=False, Config=None -> False (explicit job setting retained)
    cfg_none = create_test_policy_config(strict_hands_on_required=None)
    assert resolve_strict_hands_on_precedence(job=job, config=cfg_none) is False


def test_precedence_missing_job_flag() -> None:
    # Job without attribute / None
    job_no_attr = _DummyJob()
    cfg_true = create_test_policy_config(strict_hands_on_required=True)
    assert resolve_strict_hands_on_precedence(job=job_no_attr, config=cfg_true) is True

    cfg_false = create_test_policy_config(strict_hands_on_required=False)
    assert resolve_strict_hands_on_precedence(job=job_no_attr, config=cfg_false) is False

    # CRITICAL: When both Job and Config are missing, DO NOT assume False; return None
    cfg_none = create_test_policy_config(strict_hands_on_required=None)
    assert resolve_strict_hands_on_precedence(job=job_no_attr, config=cfg_none) is None
    assert resolve_strict_hands_on_precedence(job=job_no_attr, config=None) is None
