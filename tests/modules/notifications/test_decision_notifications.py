"""Experts hear about their blueprints and questions; candidates hear the interview flow changed."""

import asyncio
from types import SimpleNamespace

from src.modules.interviews.application import interview_blueprints
from src.modules.question_bank.notifications import decision_message, notify_question_author


class _RecordingDb:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute(self, statement, params=None):  # noqa: ANN001 - SQLAlchemy text clause
        self.calls.append({"sql": str(statement), **(params or {})})


def test_question_outcomes_quote_the_question_and_skip_intermediate_reviews() -> None:
    title, message = decision_message("REQUEST_CHANGES", "Redis   cache là gì?", "Thêm TTL")
    assert title == "Câu hỏi cần chỉnh sửa"
    assert message == "“Redis cache là gì?” cần chỉnh sửa trước khi gửi duyệt lại. Góp ý: Thêm TTL"
    assert decision_message("APPROVE", "x", None) is None
    assert "Góp ý" not in decision_message("APPROVED", "x", "ok")[1]
    assert decision_message("REJECT", "a" * 200, None)[1].startswith("“" + "a" * 79 + "…”")


def test_question_author_is_notified_once_per_outcome() -> None:
    db = _RecordingDb()
    version = SimpleNamespace(id="v1", question_id="q1", created_by="expert-1", canonical_text="Câu hỏi?")
    asyncio.run(notify_question_author(db, version, "APPROVED"))
    asyncio.run(notify_question_author(db, version, "APPROVE"))  # intermediate review: nothing
    assert len(db.calls) == 1
    call = db.calls[0]
    assert call["user_id"] == "expert-1"
    assert call["dedupe_key"] == "question:v1:APPROVED"
    assert '"link": "/admin/question-bank/q1"' in call["data"]


def _row() -> dict:
    return {"id": "bp-1", "blueprint_id": "type-1", "version": 3, "name": "Gọn", "created_by": "expert-1"}


def test_a_new_type_tells_the_author_and_the_candidates() -> None:
    db = _RecordingDb()
    asyncio.run(interview_blueprints._notify_decision(db, _row(), activate=True, note="", new_type=True))
    recipients = [(call.get("user_id"), call.get("role")) for call in db.calls]
    assert recipients == [("expert-1", None), (None, "CANDIDATE")]
    assert db.calls[0]["title"] == "Kiểu phỏng vấn đã được duyệt"
    assert db.calls[1]["dedupe_key"] == "blueprint-type:type-1:added"


def test_a_new_version_of_an_existing_type_does_not_notify_candidates() -> None:
    db = _RecordingDb()
    asyncio.run(interview_blueprints._notify_decision(db, _row(), activate=True, note=""))
    assert [call.get("user_id") for call in db.calls] == ["expert-1"]


def test_rejection_tells_only_the_author_with_the_reason() -> None:
    db = _RecordingDb()
    asyncio.run(interview_blueprints._notify_decision(db, _row(), activate=False, note="Thiếu câu hành vi"))
    assert len(db.calls) == 1
    assert db.calls[0]["user_id"] == "expert-1"
    assert db.calls[0]["message"].endswith("Lý do: Thiếu câu hành vi")
