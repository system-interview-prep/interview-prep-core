"""Chủ đề nói ra được, và câu tiếp nhận ngắn ở phần mở đầu.

Buổi thật 11/11 là nguồn của cả hai nhóm test: AI đọc nguyên nhãn nội bộ
"Xác thực CV & Dự án" rồi xin ứng viên xác nhận, và lượt làm rõ phần mở đầu chỉ
có mỗi câu hỏi trơ.
"""

import pytest

from src.modules.interviews.core.conversation.turn_rules import receipt_rejection_reason
from src.modules.interviews.core.dialogue.topics import (
    generic_transition,
    names_internal_structure,
    spoken_topic,
    transition_rejection_reason,
)


@pytest.mark.parametrize(
    "line, reason",
    [
        ("Mình chuyển sang phần Xác thực CV và Dự án, bạn xác nhận để mình bắt đầu.", "internal_label"),
        ("Mình chuyển sang phần Xác thực CV & Dự án nhé.", "internal_label"),
        ("Tiếp theo mình hỏi về Kỹ năng mềm & Tình huống (STAR) nhé.", "internal_label"),
        ("Mình sang phần Giới thiệu & Khởi động nhé.", "internal_label"),
        ("Giờ mình sang giai đoạn DEEP_DIVE nhé.", "stage_name"),
        ("Tiếp theo là stage VALIDATE.", "stage_name"),
        ("Tiếp theo là vòng 2 nhé.", "numbered_section"),
        ("Mình qua bước thứ ba nhé.", "numbered_section"),
        ("Mình còn 3 câu nữa thôi.", "progress_disclosure"),
        ("Theo kế hoạch phỏng vấn thì tiếp theo là dự án.", "plan_vocabulary"),
        ("Câu này mình chấm theo rubric có sẵn.", "plan_vocabulary"),
    ],
)
def test_interview_structure_is_never_spoken(line: str, reason: str) -> None:
    assert names_internal_structure(line) == reason
    assert transition_rejection_reason(line) == reason


@pytest.mark.parametrize(
    "line",
    [
        "Bạn xác nhận để mình bắt đầu.",
        "Bạn xác nhận giúp mình nhé.",
        "Vui lòng xác nhận trước khi mình hỏi tiếp.",
        "Please confirm to begin.",
    ],
)
def test_transition_never_asks_the_candidate_to_confirm(line: str) -> None:
    assert transition_rejection_reason(line) == "asks_confirmation"


@pytest.mark.parametrize(
    "line",
    [
        "Ok, mình hiểu rồi. Tiếp theo mình hỏi bạn về dự án HamTech nhé.",
        "Được rồi. Giờ mình hỏi bạn một câu về cách bạn làm việc cùng team nhé.",
        "Tiếp theo mình muốn hỏi bạn về Redis nhé.",
        # "Phần cuối" chỉ là nhịp nói của người phỏng vấn, không để lộ kế hoạch.
        "Phần cuối, mình hỏi về cách bạn làm việc nhóm.",
    ],
)
def test_natural_transitions_are_allowed(line: str) -> None:
    assert transition_rejection_reason(line) is None


def test_transition_is_a_signpost_not_a_question() -> None:
    assert transition_rejection_reason("Mình hỏi bạn về dự án HamTech được không?") == "is_question"


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        # Tên dự án là của ứng viên: cụ thể nhất và luôn được ưu tiên.
        (
            {"concept_id": "cv-validation", "competency": "Xác thực CV & Dự án", "project_name": "HamTech"},
            "dự án HamTech",
        ),
        ({"concept_id": "cv-validation", "competency": "Xác thực CV & Dự án"}, "dự án trong CV của bạn"),
        (
            {"concept_id": "soft-skill.star", "competency": "Kỹ năng mềm & Tình huống (STAR)"},
            "cách bạn làm việc cùng team",
        ),
        # Nhãn kỹ năng thật thì đọc lên được.
        ({"concept_id": "skill-redis", "competency": "Redis"}, "Redis"),
        # Phần mở đầu không bao giờ được báo trước như một chủ đề.
        ({"concept_id": "warmup", "competency": "Giới thiệu & Khởi động"}, None),
        # Không có concept id, chỉ có nhãn nội bộ: thà nói chung chung.
        ({"competency": "Xác thực CV & Dự án"}, None),
        ({"competency": "Chuyên môn"}, None),
    ],
)
def test_spoken_topic_never_returns_an_internal_label(kwargs: dict, expected: str | None) -> None:
    assert spoken_topic(**kwargs) == expected


def test_generic_transition_is_sayable_with_and_without_a_topic() -> None:
    with_topic = generic_transition("dự án HamTech")
    assert transition_rejection_reason(with_topic) is None
    assert "HamTech" in with_topic
    assert transition_rejection_reason(generic_transition(None)) is None
    assert transition_rejection_reason(generic_transition(None, is_vi=False)) is None


@pytest.mark.parametrize(
    "receipt",
    ["Ừm, mình hiểu rồi.", "Ừ, rõ rồi.", "Ok, mình nắm rồi.", "À, vậy à."],
)
def test_short_content_free_receipt_is_accepted(receipt: str) -> None:
    assert receipt_rejection_reason(receipt, answer="Dạ em đang là sinh viên năm cuối ở IUH") is None


@pytest.mark.parametrize(
    "receipt, reason",
    [
        # Nhắc lại nội dung là câu ghi nhận, không phải câu tiếp nhận: ở phần
        # mở đầu nó nghe như đọc lại checklist tự giới thiệu.
        ("À, vậy là bạn đang học năm cuối.", "paraphrases_answer"),
        ("Mình ghi nhận bạn là sinh viên.", "grader_style"),
        # Mở đầu buổi đã chào rồi.
        ("Chào bạn, mình bắt đầu nhé.", "greeting"),
        ("Ok.", "too_curt"),
        ("Câu trả lời của bạn rất tốt.", "evaluative"),
        ("Mình hiểu rồi. Giờ mình hỏi tiếp nhé.", "not_a_receipt"),
        ("Bạn thấy sao?", "not_a_receipt"),
        ("Mình sang phần Xác thực CV nhé.", "internal_label"),
    ],
)
def test_receipt_rejects_anything_that_is_not_a_receipt(receipt: str, reason: str) -> None:
    assert receipt_rejection_reason(receipt, answer="Dạ em đang là sinh viên năm cuối") == reason


def test_receipt_is_not_repeated_from_a_recent_turn() -> None:
    assert (
        receipt_rejection_reason("Ừm, mình hiểu rồi.", recent=["Ừm, mình hiểu rồi."], answer="Dạ em học IUH")
        == "repeats_recent"
    )
