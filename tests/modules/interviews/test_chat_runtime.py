from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
import pytest

from src.modules.interviews.chat_runtime import (
    SAFE_FALLBACK_PROBE_VI,
    ChatRuntimeError,
    _validate_probe_text,
    complete_chat_session,
    get_chat_runtime,
    process_candidate_message,
    start_chat_session,
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


class MockChatSession:
    def __init__(self, session_row: dict[str, Any], turns: list[dict[str, Any]], messages: list[dict[str, Any]] | None = None):
        self.session_row = dict(session_row)
        self.turns = {t["id"]: dict(t) for t in turns}
        self.messages = [dict(m) for m in (messages or [])]
        self.job_title = "AI Engineer"

    async def scalar(self, statement: Any, params: dict[str, Any] | None = None):
        sql = str(statement).strip()
        params = params or {}
        if "SELECT COUNT(*) FROM interview_chat_messages WHERE session_id = :sid" in sql:
            return len([m for m in self.messages if m["session_id"] == params.get("sid")])
        if "SELECT title FROM job_descriptions" in sql:
            return self.job_title
        if "SELECT COALESCE(MAX(sequence), 0)" in sql:
            seqs = [m.get("sequence", 0) for m in self.messages if m["session_id"] == params.get("sid")]
            return max(seqs) if seqs else 0
        if "SELECT COUNT(*) FROM interview_chat_messages" in sql and "message_type IN ('PROBE', 'CLARIFY')" in sql:
            matching = [
                m for m in self.messages
                if m["session_id"] == params.get("sid")
                and m.get("turn_id") == params.get("turn_id")
                and m.get("role") == "assistant"
                and m.get("message_type") in ("PROBE", "CLARIFY")
            ]
            return len(matching)
        return None

    async def execute(self, statement: Any, params: dict[str, Any] | None = None):
        sql = str(statement).strip()
        params = params or {}

        if "SELECT id, session_id, role, content" in sql and "FROM interview_chat_messages" in sql:
            if "WHERE session_id = :sid AND client_message_id = :cid" in sql:
                matching = [
                    m for m in self.messages
                    if m["session_id"] == params.get("sid")
                    and m.get("client_message_id") == params.get("cid")
                ]
                return MockResult(matching)
            if "WHERE session_id = :sid AND sequence = :seq" in sql:
                matching = [
                    m for m in self.messages
                    if m["session_id"] == params.get("sid")
                    and m.get("sequence") == params.get("seq")
                ]
                return MockResult(matching)
            matching = [m for m in self.messages if m["session_id"] == params.get("session_id")]
            matching.sort(key=lambda x: (x.get("sequence", 0), x.get("created_at") or datetime.min))
            return MockResult(matching)

        if "SELECT id, turn_index, status" in sql and "FROM interview_turns" in sql:
            matching = [t for t in self.turns.values() if t["session_id"] == params.get("session_id")]
            matching.sort(key=lambda x: x["turn_index"])
            return MockResult(matching)

        if "UPDATE interview_turns" in sql:
            turn_id = params.get("turn_id")
            if turn_id in self.turns:
                if "SET status = 'ASKED'" in sql:
                    self.turns[turn_id]["status"] = "ASKED"
                if "SET status = 'ANSWERED'" in sql:
                    self.turns[turn_id]["status"] = "ANSWERED"
                if ":ans" in sql:
                    self.turns[turn_id]["answer_text"] = params.get("ans")
            return MockResult([])

        if "INSERT INTO interview_chat_messages" in sql:
            new_msg = {
                "id": params["id"],
                "session_id": params["sid"],
                "role": "assistant" if "assistant" in sql else "user",
                "content": params["content"],
                "turn_id": params.get("turn_id"),
                "message_type": params.get("msg_type") or ("GREETING" if "GREETING" in sql else "MAIN_QUESTION" if "MAIN_QUESTION" in sql else "WRAP_UP" if "WRAP_UP" in sql else "CANDIDATE_ANSWER"),
                "sequence": params["seq"] if "seq" in params else (1 if "GREETING" in sql else 2),
                "client_message_id": params.get("cid"),
                "created_at": datetime.now(UTC),
            }
            self.messages.append(new_msg)
            return MockResult([])

        if "UPDATE interview_sessions" in sql:
            if "RETURNING chat_started_at, chat_deadline_at" in sql:
                started = self.session_row.get("chat_started_at") or datetime.now(UTC)
                deadline = self.session_row.get("chat_deadline_at") or started + timedelta(minutes=25)
                self.session_row.update(chat_started_at=started, chat_deadline_at=deadline)
                return MockResult([{"chat_started_at": started, "chat_deadline_at": deadline}])
            if "SET status = 'CLOSED'" in sql:
                self.session_row["status"] = "CLOSED"
                if "USER_ENDED" in sql or params.get("reason") == "USER_ENDED":
                    self.session_row["end_reason"] = "USER_ENDED"
                elif "COMPLETED" in sql:
                    self.session_row["end_reason"] = "COMPLETED"
                elif params.get("reason"):
                    self.session_row["end_reason"] = params.get("reason")
            return MockResult([])

        return MockResult([])

    async def commit(self):
        pass

    async def rollback(self):
        pass


@pytest.fixture
def mock_session_data():
    session_row = {
        "id": "sess-123",
        "user_id": "usr-1",
        "job_id": "job-1",
        "resume_id": "cv-1",
        "mode": "text",
        "experience_type": "interview_chat",
        "end_reason": None,
        "locale": "vi-VN",
        "status": "OPEN",
        "plan_status": "LOCKED",
    }
    turns = [
        {
            "id": "turn-1",
            "session_id": "sess-123",
            "turn_index": 0,
            "status": "PLANNED",
            "question_version_id": "qver-1",
            "question_snapshot": {
                "questionText": "Trình bày về RAG architecture?",
                "target": {"conceptId": "skill-ai"},
            },
            "answer_text": None,
        },
        {
            "id": "turn-2",
            "session_id": "sess-123",
            "turn_index": 1,
            "status": "PLANNED",
            "question_version_id": "qver-2",
            "question_snapshot": {
                "questionText": "Trình bày về Docker containerization?",
                "target": {"conceptId": "skill-devops"},
            },
            "answer_text": None,
        },
    ]
    return session_row, turns


# INV-4: Fail-fast Gate
@pytest.mark.asyncio
async def test_inv4_start_chat_session_not_locked():
    session_row = {"id": "sess-0", "status": "OPEN", "plan_status": "DRAFT"}
    db = MockChatSession(session_row, [])
    with pytest.raises(ChatRuntimeError, match="LOCKED"):
        await start_chat_session(db, session_row)


# INV-1: Sequence Monotonicity & INV-5: Turn-to-Question Provenance
@pytest.mark.asyncio
async def test_inv1_and_inv5_start_chat_session_success(mock_session_data):
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)

    runtime = await start_chat_session(db, session_row)
    assert runtime["sessionId"] == "sess-123"
    assert runtime["sessionStatus"] == "OPEN"
    assert len(runtime["messages"]) == 2

    # Check Sequence Monotonicity
    assert runtime["messages"][0]["sequence"] == 1
    assert runtime["messages"][1]["sequence"] == 2
    assert runtime["messages"][0]["messageType"] == "GREETING"
    assert runtime["messages"][1]["messageType"] == "MAIN_QUESTION"

    # Check Provenance
    assert runtime["messages"][1]["turnId"] == "turn-1"
    assert "RAG architecture" in runtime["messages"][1]["content"]
    assert db.turns["turn-1"]["status"] == "ASKED"


# INV-2: Idempotency Guarantee
@pytest.mark.asyncio
async def test_inv2_idempotency_duplicate_client_message(mock_session_data):
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    with patch("src.modules.interviews.chat_runtime.generate_text", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = '{"decision": "PROBE", "reply_text": "Bạn tối ưu latency như thế nào?"}'

        res1 = await process_candidate_message(
            db,
            session_row,
            client_message_id="msg-client-duplicate",
            content="Tôi dùng FAISS index và rerank bằng cross-encoder.",
        )
        assert res1["userMessage"]["sequence"] == 3
        assert res1["assistantResponse"]["sequence"] == 4

        # Duplicate call with exact same client_message_id
        res2 = await process_candidate_message(
            db,
            session_row,
            client_message_id="msg-client-duplicate",
            content="Tôi dùng FAISS index và rerank bằng cross-encoder.",
        )
        # Should return existing messages, no new messages created
        assert res2["userMessage"]["sequence"] == 3
        assert res2["assistantResponse"]["sequence"] == 4
        assert len(db.messages) == 4


# INV-3: Probe Budget Cap (Never exceed 1 probe per turn)
@pytest.mark.asyncio
async def test_inv3_probe_budget_cap(mock_session_data):
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    # 1. Answer 1 -> PROBE
    with patch("src.modules.interviews.chat_runtime.generate_text", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = '{"decision": "PROBE", "reply_text": "Follow-up question 1"}'
        await process_candidate_message(
            db,
            session_row,
            client_message_id="msg-client-1",
            content="Answer 1",
        )

    # 2. Answer 2 (Probe answer) -> Budget reached -> Must advance to NEXT_TOPIC
    res2 = await process_candidate_message(
        db,
        session_row,
        client_message_id="msg-client-2",
        content="Answer 2: chi tiết thêm",
    )

    assert res2["assistantResponse"]["messageType"] == "MAIN_QUESTION"
    assert "Docker containerization" in res2["assistantResponse"]["content"]
    assert res2["turnStatus"]["isFollowUp"] is False
    assert db.turns["turn-1"]["status"] == "ANSWERED"
    assert db.turns["turn-2"]["status"] == "ASKED"


# Deterministic Post-Validator for Probe
def test_probe_post_validator():
    # Valid probe
    assert _validate_probe_text("Bạn có thể giải thích rõ hơn về cách chia chunk không?") is True
    # Too long
    assert _validate_probe_text("A" * 300) is False
    # Score / rubric leak prohibited
    assert _validate_probe_text("Câu này bạn đạt 8/10 điểm, tiêu chí tiếp theo là gì?") is False
    assert _validate_probe_text("Theo rubric đánh giá của chúng tôi...") is False


@pytest.mark.asyncio
async def test_probe_post_validator_safe_fallback(mock_session_data):
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    # LLM returns a response that leaks score
    with patch("src.modules.interviews.chat_runtime.generate_text", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = '{"decision": "PROBE", "reply_text": "Bạn được 7/10 điểm câu này, bạn muốn nói gì thêm không?"}'

        res = await process_candidate_message(
            db,
            session_row,
            client_message_id="msg-client-leak",
            content="Tôi đã làm dự án này trong 2 năm.",
        )

        # Validator triggers safe fallback template
        assert res["assistantResponse"]["content"] == SAFE_FALLBACK_PROBE_VI
        assert "7/10" not in res["assistantResponse"]["content"]


# INV-6: Privacy & Rubric Containment
@pytest.mark.asyncio
async def test_inv6_privacy_and_rubric_containment(mock_session_data):
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    runtime = await start_chat_session(db, session_row)

    # Inspect payload: must not contain rubricVersionId, criteria, or rawText
    runtime_str = str(runtime)
    assert "rubricVersionId" not in runtime_str
    assert "criteria" not in runtime_str
    assert "scoreMax" not in runtime_str


@pytest.mark.asyncio
async def test_early_exit_sets_user_ended(mock_session_data):
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    res = await process_candidate_message(
        db,
        session_row,
        client_message_id="msg-client-quit",
        content="Tôi muốn dừng phỏng vấn tại đây.",
    )
    assert res["assistantResponse"]["messageType"] == "WRAP_UP"
    assert res["sessionStatus"] == "CLOSED"
    assert res["endReason"] == "USER_ENDED"
    assert db.session_row["end_reason"] == "USER_ENDED"


@pytest.mark.asyncio
async def test_complete_chat_session_with_reason(mock_session_data):
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    res = await complete_chat_session(db, session_row, reason="USER_ENDED")
    assert res["sessionStatus"] == "CLOSED"
    assert res["endReason"] == "USER_ENDED"
    assert "Phiên phỏng vấn đã kết thúc" in res["summary"]
    assert db.session_row["status"] == "CLOSED"


@pytest.mark.asyncio
async def test_start_rejects_incomplete_frozen_question(mock_session_data):
    session_row, turns = mock_session_data
    turns[0]["question_snapshot"] = {}
    db = MockChatSession(session_row, turns)
    with pytest.raises(ChatRuntimeError, match="Frozen question is incomplete"):
        await start_chat_session(db, session_row)
    assert db.messages == []


@pytest.mark.asyncio
async def test_chat_rejects_practice_session(mock_session_data):
    session_row, turns = mock_session_data
    session_row["experience_type"] = "question_practice"
    db = MockChatSession(session_row, turns)
    with pytest.raises(ChatRuntimeError, match="not an interview chat"):
        await start_chat_session(db, session_row)


@pytest.mark.asyncio
async def test_retry_key_requires_same_content(mock_session_data):
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)
    with patch("src.modules.interviews.chat_runtime.generate_text", new_callable=AsyncMock) as llm:
        llm.return_value = '{"decision": "NEXT_TOPIC", "reply_text": ""}'
        await process_candidate_message(
            db, session_row, client_message_id="same-key",
            content="My first substantive answer about retrieval.",
        )
    with pytest.raises(ChatRuntimeError, match="different content"):
        await process_candidate_message(
            db, session_row, client_message_id="same-key",
            content="A different answer.",
        )


@pytest.mark.asyncio
async def test_retry_pending_response_is_explicit(mock_session_data):
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)
    db.messages.append({
        "id": "pending-user", "session_id": session_row["id"],
        "role": "user", "content": "My answer", "turn_id": "turn-1",
        "message_type": "CANDIDATE_ANSWER", "sequence": 3,
        "client_message_id": "pending-key", "created_at": datetime.now(UTC),
    })
    with pytest.raises(ChatRuntimeError, match="response is pending"):
        await process_candidate_message(
            db, session_row, client_message_id="pending-key", content="My answer",
        )
