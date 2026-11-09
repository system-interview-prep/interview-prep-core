"""Which JD requirement lines are allowed to disqualify a candidate."""

from __future__ import annotations

import pytest

from src.modules.matching.domain.requirement_gating import classify_requirement_gate


@pytest.mark.parametrize(
    "label",
    [
        "No experience required",
        "Không yêu cầu kinh nghiệm",
        "Không cần kinh nghiệm",
    ],
)
def test_negated_requirement_is_vacuous_and_never_gates(label: str) -> None:
    """A waived condition must not be re-read as a condition to prove."""
    decision = classify_requirement_gate(label, "experience")
    assert decision.gate_class == "vacuous"
    assert not decision.may_gate
    assert not decision.is_scorable


@pytest.mark.parametrize(
    "label",
    [
        "Độ tuổi từ 23 đến 55 tuổi",
        "Đủ sức khỏe để đảm nhận công việc",
        "Ngoại hình ưa nhìn",
        "Nam/Nữ, tốt nghiệp phổ thông",
    ],
)
def test_protected_or_physical_attributes_never_gate(label: str) -> None:
    decision = classify_requirement_gate(label, None)
    assert decision.gate_class == "non_assessable"
    assert not decision.may_gate
    assert not decision.is_scorable


@pytest.mark.parametrize(
    "label",
    [
        "Năng động, vui vẻ và thân thiện",
        "Nhiệt tình",
        "Good problem-solving, analytical, and evaluative skills",
        "Có tư duy User-centric",
        "Cẩn thận, tỉ mỉ trong công việc",
        "Chịu được áp lực công việc cao",
    ],
)
def test_attitude_requirements_score_but_do_not_gate(label: str) -> None:
    """A CV cannot prove enthusiasm, so it may inform the score, never the gate."""
    decision = classify_requirement_gate(label, None)
    assert decision.gate_class == "soft_signal"
    assert not decision.may_gate
    assert decision.is_scorable


@pytest.mark.parametrize(
    "label",
    [
        "Thành thạo Java và Spring Boot",
        "Tối thiểu 2 năm kinh nghiệm Python",
        "Tốt nghiệp Đại học ngành Công nghệ thông tin",
        "IELTS 6.0 hoặc tương đương",
        "Knowledge of OOP/OOD and design patterns",
        "Có kỹ năng memory management cơ bản",
    ],
)
def test_checkable_conditions_keep_their_gate(label: str) -> None:
    decision = classify_requirement_gate(label, None)
    assert decision.gate_class == "objective"
    assert decision.may_gate


@pytest.mark.parametrize(
    "label",
    [
        "Good English communication skills, especially in writing",
        "Khả năng đọc hiểu tài liệu tiếng Anh tốt và giao tiếp cơ bản",
        "Khả năng giao tiếp và đọc tài liệu tiếng Anh",
    ],
)
def test_named_language_stays_objective_despite_communication_wording(label: str) -> None:
    """Language ability is evidenced from the CV, unlike a personality wish.

    These lines read like soft skills ("communication skills") but state a
    checkable language requirement, so the communication wording must not
    strip their gate.
    """
    decision = classify_requirement_gate(label, None)
    assert decision.gate_class == "objective"
    assert decision.may_gate


@pytest.mark.parametrize(
    "label",
    [
        # "op-timi-zation" must not match the "tỉ mỉ" (meticulous) pattern.
        "Know database design, performance tuning, and optimization",
        # "kỹ năng memory" must not match the "kỹ năng mềm" (soft skills) one.
        "Có kỹ năng memory management cơ bản",
    ],
)
def test_technical_terms_are_not_mistaken_for_soft_skills(label: str) -> None:
    assert classify_requirement_gate(label, None).gate_class == "objective"


def test_hard_condition_survives_adjacent_attitude_wording() -> None:
    """A line may ask for a degree and enthusiasm at once; the degree still gates."""
    decision = classify_requirement_gate(
        "Tốt nghiệp Đại học CNTT, nhiệt tình và chịu khó", None
    )
    assert decision.gate_class == "objective"
    assert decision.may_gate


def test_negation_does_not_waive_a_stated_hard_condition() -> None:
    decision = classify_requirement_gate(
        "Không yêu cầu kinh nghiệm nhưng phải có bằng Đại học", None
    )
    assert decision.gate_class == "objective"


def test_parser_kind_does_not_override_the_text() -> None:
    """The JD parser's ``kind`` is unreliable, so the text decides.

    Observed in production: health and attitude lines arrive tagged
    ``language``, education lines arrive tagged ``skill``.
    """
    assert classify_requirement_gate("Đủ sức khỏe để đảm nhận công việc", "language").gate_class == (
        "non_assessable"
    )
    assert classify_requirement_gate("Năng động, vui vẻ và thân thiện", "skill").gate_class == (
        "soft_signal"
    )


def test_empty_requirement_text_cannot_gate() -> None:
    assert not classify_requirement_gate("", "skill").may_gate
