"""The chat room's quick replies (frontend room.dict.ts, ChatRoomParts.quickRepliesFor)
must keep meaning what the room promises: change a phrase there, update it here."""

from src.modules.interviews.core.interview_engine import (
    classify_candidate_intent,
    classify_candidate_question,
    is_repeat_request,
)

QUESTION = "Lỗi lan truyền thế nào qua promise chain và async/await?"


def test_clarify_chip_is_a_clarification_not_an_answer() -> None:
    text = "Mình chưa rõ ý câu hỏi, bạn giải thích thêm được không?"
    assert classify_candidate_intent(text, QUESTION) == "CLARIFY_REQUEST"
    assert not is_repeat_request(text)


def test_confirmed_skip_phrase_is_a_skip() -> None:
    assert classify_candidate_intent("Cho mình đổi sang câu hỏi khác được không?", QUESTION) == "SKIP_REQUEST"


def test_role_question_chip_is_a_question_about_the_role() -> None:
    assert classify_candidate_question("Công việc hằng ngày của vị trí này như thế nào?") == "ROLE_QA"


def test_long_self_introduction_is_not_misclassified_as_a_role_question() -> None:
    introduction = (
        "Dạ em là sinh viên năm cuối ngành Công nghệ thông tin tại Đại học Công nghiệp "
        "Thành phố Hồ Chí Minh. "
        "Em đang ứng tuyển vị trí Backend Developer. Em thích làm backend vì muốn hiểu cách một yêu cầu từ "
        "người dùng đi qua API, xử lý nghiệp vụ rồi được lưu vào cơ sở dữ liệu như thế nào. Gần đây em làm "
        "một dự án trợ lý học tập có dùng AI, trong đó em phụ trách một phần API và xử lý dữ liệu."
    )
    assert len(introduction.split()) > 20
    assert classify_candidate_question(introduction) is None
    assert (
        classify_candidate_intent(introduction, "Bạn giới thiệu ngắn về bản thân nhé?")
        == "VALID_ANSWER"
    )


def test_phrases_left_out_of_the_chips_would_be_graded_as_answers() -> None:
    # Why the room offers no "think more" / "give me an example" chip.
    for text in ("Cho mình suy nghĩ thêm một chút nhé", "Bạn cho mình một ví dụ cụ thể được không?"):
        assert classify_candidate_intent(text, QUESTION) == "VALID_ANSWER"
