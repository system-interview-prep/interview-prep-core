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


# The cases below were taken from the 99 requirements the v6 JD parser produced
# for ten real postings.  They were invisible while the parser collapsed a whole
# requirements block into one row: the collapsed row inherited ``preferred`` from
# any one constituent, so nothing downstream ever saw them as must-haves.  Once
# each claim became its own row, 61 of the 99 were allowed to disqualify people,
# including every line below.


@pytest.mark.parametrize(
    "label",
    [
        "Yêu thích công nghệ",
        "Yêu thích công việc DBA",
        "Want to develop in data analysis",
        "Có mong muốn phát triển trong ngành phân tích dữ liệu",
    ],
)
def test_an_aspiration_is_not_a_qualification(label: str) -> None:
    """Wanting the job is not evidence of being able to do it."""
    decision = classify_requirement_gate(label, "other")
    assert decision.gate_class == "soft_signal"
    assert not decision.may_gate
    assert decision.is_scorable


@pytest.mark.parametrize(
    "label",
    [
        "Chịu khó tìm tòi và cập nhật kiến thức công nghệ mới",
        "Chịu khó học hỏi",
        "Ham học hỏi",
    ],
)
def test_diligence_wording_scores_but_does_not_gate(label: str) -> None:
    decision = classify_requirement_gate(label, "other")
    assert decision.gate_class == "soft_signal"
    assert not decision.may_gate


@pytest.mark.parametrize(
    "label",
    [
        "Có khả năng làm việc độc lập hoặc theo nhóm",
        "Ability to work independently and within a team",
        "Ability to collaborate with others",
        "Ability to prioritize and handle multiple tasks simultaneously",
        "Flexibility to handle multiple assignments",
    ],
)
def test_working_style_claims_do_not_gate(label: str) -> None:
    """How someone works with others is settled at interview, not on paper."""
    decision = classify_requirement_gate(label, "skill")
    assert decision.gate_class == "soft_signal"
    assert not decision.may_gate


@pytest.mark.parametrize(
    "label",
    [
        "Giọng nói rõ ràng, dễ nghe",
        "Phát âm chuẩn, không nói ngọng",
    ],
)
def test_voice_is_never_evidenced_by_a_document(label: str) -> None:
    """A real condition for a phone role - and unanswerable from a CV."""
    decision = classify_requirement_gate(label, "other")
    assert decision.gate_class == "non_assessable"
    assert not decision.may_gate
    assert not decision.is_scorable


@pytest.mark.parametrize(
    "label",
    [
        "Có laptop",
        "Có xe máy để di chuyển",
        "Own a laptop",
    ],
)
def test_equipment_is_a_condition_but_not_a_cv_claim(label: str) -> None:
    """A candidate who owns a laptop is under no obligation to write it down."""
    decision = classify_requirement_gate(label, "other")
    assert decision.gate_class == "non_assessable"
    assert not decision.may_gate
    assert not decision.is_scorable


def test_owning_a_laptop_differs_from_knowing_laptops() -> None:
    """The word alone must not demote a product-knowledge requirement."""
    decision = classify_requirement_gate(
        "Am hiểu các hệ điều hành điện thoại và laptop", "skill"
    )
    assert decision.gate_class == "objective"
    assert decision.may_gate


@pytest.mark.parametrize(
    "label",
    [
        "Chấp nhận sinh viên chưa có kinh nghiệm",
        "Tiếp nhận ứng viên chưa có kinh nghiệm",
    ],
)
def test_accepting_the_inexperienced_waives_the_condition(label: str) -> None:
    """Read as a must-have, this rejects the people it was written to invite."""
    decision = classify_requirement_gate(label, "experience")
    assert decision.gate_class == "vacuous"
    assert not decision.may_gate
    assert not decision.is_scorable


@pytest.mark.parametrize(
    "label",
    [
        "Có kiến thức về API tích hợp là 1 lợi thế",
        "Có khả năng viết Procedure hoặc Function là 1 lợi thế",
        "Biết thêm Docker là một điểm cộng",
        "Ưu tiên ứng viên biết tiếng Nhật",
        "Experience with Kubernetes is a plus",
        "Knowledge of Figma is nice to have",
    ],
)
def test_a_posting_that_calls_an_item_a_bonus_cannot_gate_on_it(label: str) -> None:
    """The extractor's priority must not outrank the sentence it came from.

    These name concrete, checkable skills, so they keep scoring - unlike an
    attitude line, there really is CV evidence for them.  What they must not do
    is disqualify anyone, because the posting says they are optional.
    """
    decision = classify_requirement_gate(label, "skill")
    assert decision.gate_class == "optional"
    assert not decision.may_gate
    assert decision.is_scorable


def test_optionality_is_decided_after_assessability() -> None:
    """"Nam, ưu tiên có kinh nghiệm" is still a protected attribute first."""
    decision = classify_requirement_gate("Nam, ưu tiên có kinh nghiệm bán hàng", None)
    assert decision.gate_class == "non_assessable"
    assert not decision.is_scorable


def test_broadened_mindset_wording_still_yields_to_a_concrete_anchor() -> None:
    """"tư duy" alone is a soft claim; "tư duy thuật toán" names a subject."""
    assert classify_requirement_gate("Có tư duy lập trình, logic tốt", None).gate_class == (
        "soft_signal"
    )
    assert classify_requirement_gate(
        "Có tư duy về thuật toán và cấu trúc dữ liệu", None
    ).gate_class == "objective"


@pytest.mark.parametrize(
    "label",
    [
        "Nam, ưu tiên có kinh nghiệm bán hàng",
        "Nữ: ngoại hình ưa nhìn",
    ],
)
def test_a_bare_gender_opening_a_requirement_is_still_a_protected_attribute(
    label: str,
) -> None:
    """Only the "Nam/Nữ" pair was recognised; a lone one was not."""
    decision = classify_requirement_gate(label, None)
    assert decision.gate_class == "non_assessable"
    assert not decision.may_gate


@pytest.mark.parametrize(
    "label",
    [
        "Có 3 năm kinh nghiệm Java",
        "Tối thiểu 5 năm kinh nghiệm lập trình",
        "Năm cuối chuyên ngành Công nghệ thông tin",
        "Nắm vững SQL",
    ],
)
def test_folding_nam_does_not_swallow_durations(label: str) -> None:
    """"năm" folds to "nam": the gender rule must not reach a year count."""
    assert classify_requirement_gate(label, None).gate_class == "objective"


@pytest.mark.parametrize(
    "label",
    [
        "Nền tảng lập trình vững chắc; năng khiếu toán học là lợi thế",
        "Tiếng Anh giao tiếp tốt, có chứng chỉ là lợi thế",
        "Thành thạo SQL, biết thêm Oracle là một lợi thế",
    ],
)
def test_a_bonus_clause_does_not_waive_the_condition_beside_it(label: str) -> None:
    """The marker qualifies its own clause, not the requirement around it.

    Demoting the whole line would waive a stated condition - the mirror of the
    mistake this module exists to prevent.
    """
    decision = classify_requirement_gate(label, "skill")
    assert decision.gate_class == "objective"
    assert decision.may_gate


def test_a_leading_marker_still_covers_the_clauses_after_it() -> None:
    decision = classify_requirement_gate(
        "Ưu tiên ứng viên biết tiếng Nhật, có kinh nghiệm 2 năm", "skill"
    )
    assert decision.gate_class == "optional"
    assert not decision.may_gate
