from datetime import UTC, datetime
from typing import Any
import pytest

from src.modules.interviews.text_runtime import (
    TurnStateError,
    _turn_payload,
    ask_turn,
    answer_turn,
    complete_text_runtime,
    read_text_runtime,
)


class MockResult:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def one_or_none(self):
        return self._rows[0] if self._rows else None


class MockAsyncSession:
    def __init__(self, turns: list[dict[str, Any]]):
        self.turns = {t["id"]: dict(t) for t in turns}
        self.session_updates = []
        self.committed = False

    async def execute(self, statement: Any, params: dict[str, Any] | None = None):
        sql = str(statement).strip()
        params = params or {}

        if "SELECT id, session_id, turn_index, status" in sql and "WHERE id = :turn_id" in sql:
            turn = self.turns.get(params.get("turn_id"))
            if turn and turn.get("session_id") == params.get("session_id"):
                return MockResult([dict(turn)])
            return MockResult([])

        if "SELECT id, session_id, turn_index, status" in sql and "WHERE session_id = :session_id" in sql:
            matching = [
                dict(t) for t in self.turns.values()
                if t.get("session_id") == params.get("session_id")
            ]
            matching.sort(key=lambda x: x["turn_index"])
            return MockResult(matching)

        if "UPDATE interview_turns" in sql and "SET status = 'ASKED'" in sql:
            turn_id = params["turn_id"]
            if turn_id in self.turns and self.turns[turn_id]["status"] == "PLANNED":
                self.turns[turn_id]["status"] = "ASKED"
                self.turns[turn_id]["started_at"] = self.turns[turn_id].get("started_at") or datetime.now(UTC)
            return MockResult([])

        if "UPDATE interview_turns" in sql and "SET status = 'ANSWERED'" in sql:
            turn_id = params["turn_id"]
            if turn_id in self.turns and self.turns[turn_id]["status"] == "ASKED":
                self.turns[turn_id]["status"] = "ANSWERED"
                self.turns[turn_id]["answer_text"] = params["answer_text"]
                self.turns[turn_id]["completed_at"] = self.turns[turn_id].get("completed_at") or datetime.now(UTC)
            return MockResult([])

        if "UPDATE interview_sessions" in sql and "SET status = 'CLOSED'" in sql:
            self.session_updates.append(params["session_id"])
            return MockResult([])

        return MockResult([])

    async def commit(self):
        self.committed = True


def test_turn_payload_exposes_frozen_question_and_answer_lifecycle():
    started = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
    completed = datetime(2026, 9, 25, 8, 2, tzinfo=UTC)
    payload = _turn_payload(
        {
            "id": "turn-1",
            "turn_index": 0,
            "status": "ANSWERED",
            "question_version_id": None,
            "rubric_version_id": None,
            "question_snapshot": {
                "questionText": "Explain dependency injection.",
                "objective": "Assess DI fundamentals.",
            },
            "answer_text": "It separates construction from use.",
            "started_at": started,
            "completed_at": completed,
        }
    )

    assert payload["turnId"] == "turn-1"
    assert payload["status"] == "ANSWERED"
    assert payload["question"]["questionText"] == "Explain dependency injection."
    assert payload["answerText"] == "It separates construction from use."
    assert payload["startedAt"] == started.isoformat()
    assert payload["completedAt"] == completed.isoformat()


@pytest.mark.asyncio
async def test_read_text_runtime_rejects_non_text_or_unlocked_plan():
    db = MockAsyncSession([])

    # Rejects non-text
    with pytest.raises(TurnStateError, match="requires a text session"):
        await read_text_runtime(
            db=db,
            session_row={"id": "s-1", "mode": "video", "plan_status": "LOCKED", "status": "OPEN"},
        )

    # Rejects DRAFT plan
    with pytest.raises(TurnStateError, match="plan must be LOCKED"):
        await read_text_runtime(
            db=db,
            session_row={"id": "s-1", "mode": "text", "plan_status": "DRAFT", "status": "OPEN"},
        )

    # Rejects READY plan (must be LOCKED by P2 selector first)
    with pytest.raises(TurnStateError, match="plan must be LOCKED"):
        await read_text_runtime(
            db=db,
            session_row={"id": "s-1", "mode": "text", "plan_status": "READY", "status": "OPEN"},
        )


@pytest.mark.asyncio
async def test_read_text_runtime_rejects_empty_turns():
    db = MockAsyncSession([])
    with pytest.raises(TurnStateError, match="Interview has no frozen turns"):
        await read_text_runtime(
            db=db,
            session_row={"id": "s-1", "mode": "text", "plan_status": "LOCKED", "status": "OPEN"},
        )


@pytest.mark.asyncio
async def test_ask_turn_state_machine_and_idempotency():
    session_row = {"id": "s-1", "mode": "text", "plan_status": "LOCKED", "status": "OPEN"}
    t1 = {
        "id": "t-1",
        "session_id": "s-1",
        "turn_index": 0,
        "status": "PLANNED",
        "question_version_id": "qv-1",
        "rubric_version_id": None,
        "question_snapshot": {"questionText": "Question 1"},
        "answer_text": None,
        "started_at": None,
        "completed_at": None,
    }
    t2 = {
        "id": "t-2",
        "session_id": "s-1",
        "turn_index": 1,
        "status": "PLANNED",
        "question_version_id": "qv-2",
        "rubric_version_id": None,
        "question_snapshot": {"questionText": "Question 2"},
        "answer_text": None,
        "started_at": None,
        "completed_at": None,
    }
    db = MockAsyncSession([t1, t2])

    # Cannot ask t-2 before t-1
    with pytest.raises(TurnStateError, match="Only the current interview turn can be asked"):
        await ask_turn(db=db, session_row=session_row, turn_id="t-2")

    # Ask t-1 (PLANNED -> ASKED)
    res1 = await ask_turn(db=db, session_row=session_row, turn_id="t-1")
    assert res1["status"] == "ASKED"
    assert db.turns["t-1"]["status"] == "ASKED"

    # Idempotent re-ask of t-1
    res1_again = await ask_turn(db=db, session_row=session_row, turn_id="t-1")
    assert res1_again["status"] == "ASKED"

    # Reject on closed session
    closed_session = {**session_row, "status": "CLOSED"}
    with pytest.raises(TurnStateError, match="Interview session is closed"):
        await ask_turn(db=db, session_row=closed_session, turn_id="t-1")


@pytest.mark.asyncio
async def test_answer_turn_validation_and_immutability():
    session_row = {"id": "s-1", "mode": "text", "plan_status": "LOCKED", "status": "OPEN"}
    t1 = {
        "id": "t-1",
        "session_id": "s-1",
        "turn_index": 0,
        "status": "ASKED",
        "question_version_id": "qv-1",
        "rubric_version_id": None,
        "question_snapshot": {"questionText": "Question 1"},
        "answer_text": None,
        "started_at": datetime.now(UTC),
        "completed_at": None,
    }
    db = MockAsyncSession([t1])

    # Reject blank answer
    with pytest.raises(ValueError, match="answerText must not be blank"):
        await answer_turn(db=db, session_row=session_row, turn_id="t-1", answer_text="   ")

    # Submit valid answer
    res = await answer_turn(db=db, session_row=session_row, turn_id="t-1", answer_text="My solid answer")
    assert res["status"] == "ANSWERED"
    assert res["answerText"] == "My solid answer"
    assert db.turns["t-1"]["status"] == "ANSWERED"

    # Double submit with identical answer is idempotent
    res_idempotent = await answer_turn(db=db, session_row=session_row, turn_id="t-1", answer_text="My solid answer")
    assert res_idempotent["status"] == "ANSWERED"
    assert res_idempotent["answerText"] == "My solid answer"

    # Double submit with different answer is rejected (immutable)
    with pytest.raises(TurnStateError, match="Answered turn is immutable"):
        await answer_turn(db=db, session_row=session_row, turn_id="t-1", answer_text="Changed my mind")


@pytest.mark.asyncio
async def test_complete_interview_lifecycle():
    session_row = {"id": "s-1", "mode": "text", "plan_status": "LOCKED", "status": "OPEN"}
    t1 = {
        "id": "t-1",
        "session_id": "s-1",
        "turn_index": 0,
        "status": "PLANNED",
        "question_version_id": "qv-1",
        "rubric_version_id": None,
        "question_snapshot": {"questionText": "Q1"},
        "answer_text": None,
        "started_at": None,
        "completed_at": None,
    }
    db = MockAsyncSession([t1])

    # Cannot complete while turns remain unanswered
    with pytest.raises(TurnStateError, match="cannot complete while turns remain unanswered"):
        await complete_text_runtime(db=db, session_row=session_row)

    # Ask turn 1
    await ask_turn(db=db, session_row=session_row, turn_id="t-1")

    # Answer turn 1
    await answer_turn(db=db, session_row=session_row, turn_id="t-1", answer_text="Answer 1")

    # Now complete should succeed
    completed = await complete_text_runtime(db=db, session_row=session_row)
    assert completed["completed"] is True
    assert completed["sessionStatus"] == "CLOSED"
    assert "s-1" in db.session_updates
