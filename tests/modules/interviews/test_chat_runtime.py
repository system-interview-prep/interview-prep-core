import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
import pytest

from src.modules.interviews.application.chat_runtime import (
    SAFE_FALLBACK_PROBE_VI,
    ChatRuntimeError,
    _validate_probe_text,
    complete_chat_session,
    get_chat_runtime,
    process_candidate_message,
    start_chat_session,
)
from src.modules.interviews.core.interview_engine import InterviewCoreEngine


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
        if "SELECT status FROM interview_sessions" in sql:
            return self.session_row.get("status")
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

        if "FROM interview_chat_messages" in sql:
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
            if "message_type IN ('PROBE', 'CLARIFY')" in sql:
                matching = [
                    m for m in self.messages
                    if m["session_id"] == params.get("sid")
                    and m.get("role") == "assistant"
                    and m.get("message_type") in ("PROBE", "CLARIFY")
                ]
                return MockResult(matching)
            if "ORDER BY sequence DESC LIMIT 1" in sql:
                sid = params.get("sid") or params.get("session_id")
                matching = [m for m in self.messages if m["session_id"] == sid]
                if "role = 'assistant'" in sql:
                    matching = [m for m in matching if m.get("role") == "assistant"]
                matching.sort(key=lambda x: x.get("sequence", 0), reverse=True)
                return MockResult([matching[0]] if matching else [])
            sid = params.get("session_id") or params.get("sid")
            matching = [m for m in self.messages if m["session_id"] == sid]
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

        if "INSERT INTO interview_turns" in sql:
            new_turn = {
                "id": params["id"],
                "session_id": params["sid"],
                "turn_index": params["tidx"],
                "status": "ASKED",
                "question_snapshot": json.loads(params["snap"]) if isinstance(params.get("snap"), str) else params.get("snap", {}),
                "answer_text": None,
                "question_version_id": None,
            }
            self.turns[params["id"]] = new_turn
            return MockResult([])

        if "FROM interview_sessions" in sql:
            return MockResult([{"status": self.session_row.get("status"), "end_reason": self.session_row.get("end_reason")}])

        if "INSERT INTO interview_chat_messages" in sql:
            cid = params.get("cid")
            if cid and any(m.get("client_message_id") == cid for m in self.messages):
                from sqlalchemy.exc import IntegrityError
                raise IntegrityError(
                    "duplicate key value violates unique constraint 'uq_interview_chat_messages_client_message_id'",
                    params,
                    None,
                )
            new_msg = {
                "id": params["id"],
                "session_id": params["sid"],
                "role": "assistant" if "assistant" in sql else "user",
                "content": params["content"],
                "turn_id": params.get("turn_id"),
                "message_type": params.get("msg_type") or ("GREETING" if "GREETING" in sql else "MAIN_QUESTION" if "MAIN_QUESTION" in sql else "WRAP_UP" if "WRAP_UP" in sql else "CANDIDATE_ANSWER"),
                "sequence": params["seq"] if "seq" in params else (1 if "'MAIN_QUESTION', 1" in sql or "GREETING" in sql else 2),
                "client_message_id": params.get("cid"),
                "created_at": datetime.now(UTC),
            }
            self.messages.append(new_msg)
            return MockResult([])

        if "UPDATE interview_sessions" in sql:
            if "metadata" in sql and params.get("meta"):
                self.session_row["metadata"] = params.get("meta")
            if "SET status = 'CLOSED'" in sql:
                self.session_row["status"] = "CLOSED"
                if params.get("reason"):
                    self.session_row["end_reason"] = params.get("reason")
                elif "USER_ENDED" in sql:
                    self.session_row["end_reason"] = "USER_ENDED"
                elif "COMPLETED" in sql:
                    self.session_row["end_reason"] = "COMPLETED"
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
    assert len(runtime["messages"]) == 1

    # Check Sequence Monotonicity
    assert runtime["messages"][0]["sequence"] == 1
    assert runtime["messages"][0]["messageType"] == "MAIN_QUESTION"

    # Check Provenance
    assert runtime["messages"][0]["turnId"] == "turn-1"
    assert "RAG architecture" in runtime["messages"][0]["content"]
    assert db.turns["turn-1"]["status"] == "ASKED"


# INV-2: Idempotency Guarantee
@pytest.mark.asyncio
async def test_inv2_idempotency_duplicate_client_message(mock_session_data):
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    with patch("src.modules.interviews.application.chat_runtime.generate_text", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = '{"decision": "PROBE", "reply_text": "Bạn tối ưu latency như thế nào?"}'

        res1 = await process_candidate_message(
            db,
            session_row,
            client_message_id="msg-client-duplicate",
            content="Tôi dùng FAISS index và rerank bằng cross-encoder.",
        )
        assert res1["userMessage"]["sequence"] == 2
        assert res1["assistantResponse"]["sequence"] == 3

        # Duplicate call with exact same client_message_id
        res2 = await process_candidate_message(
            db,
            session_row,
            client_message_id="msg-client-duplicate",
            content="Tôi dùng FAISS index và rerank bằng cross-encoder.",
        )
        # Should return existing messages, no new messages created
        assert res2["userMessage"]["sequence"] == 2
        assert res2["assistantResponse"]["sequence"] == 3
        assert len(db.messages) == 3


# INV-3: Probe Budget Cap (Never exceed 1 probe per turn)
@pytest.mark.asyncio
async def test_inv3_probe_budget_cap(mock_session_data):
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    # 1. Answer 1 -> PROBE
    with patch("src.modules.interviews.application.chat_runtime.generate_text", new_callable=AsyncMock) as mock_llm:
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
    with patch("src.modules.interviews.application.chat_runtime.generate_text", new_callable=AsyncMock) as mock_llm:
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

    # 1. Ứng viên gửi tin nhắn muốn dừng -> Backend kích hoạt CONFIRM_ABORT để mở Modal
    res = await process_candidate_message(
        db,
        session_row,
        client_message_id="msg-client-quit",
        content="Tôi muốn dừng phỏng vấn tại đây.",
    )
    assert res["action"] == "CONFIRM_ABORT"
    assert res["assistantResponse"]["messageType"] == "CONFIRM_ABORT"
    assert "hộp thoại xác nhận" in res["assistantResponse"]["content"]
    assert res["sessionStatus"] == "OPEN"

    # 2. Ứng viên xác nhận dừng trên Modal -> Đóng session với USER_ENDED
    close_res = await complete_chat_session(db, session_row, reason="USER_ENDED")
    assert close_res["sessionStatus"] == "CLOSED"
    assert close_res["endReason"] == "USER_ENDED"
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
async def test_two_consecutive_give_up_messages_trigger_early_exit(mock_session_data):
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    # First explicit Give Up increments the canonical counter and advances.
    res1 = await process_candidate_message(
        db,
        session_row,
        client_message_id="giveup-1",
        content="ừm",
    )
    assert res1["sessionStatus"] == "OPEN"
    assert res1["assistantResponse"]["messageType"] == "MAIN_QUESTION"

    first_meta = db.session_row["metadata"]
    if isinstance(first_meta, str):
        first_meta = json.loads(first_meta)
    assert first_meta["consecutive_uncooperative"] == 1
    assert first_meta["consecutive_fails"] == 1

    # Second explicit Give Up in Technical triggers FAST_FAIL_TECH internally.
    res2 = await process_candidate_message(
        db,
        session_row,
        client_message_id="giveup-2",
        content="không biết",
    )
    assert res2["sessionStatus"] == "CLOSED"
    assert res2["assistantResponse"]["messageType"] == "WRAP_UP"
    assert res2["endReason"] == "FAST_FAIL_TECH"
    assert "kết thúc phiên phỏng vấn tại đây" in res2["assistantResponse"]["content"]
    assert db.session_row["status"] == "CLOSED"
    assert db.session_row["end_reason"] == "FAST_FAIL_TECH"

    # Reload via get_chat_runtime returns persisted FAST_FAIL_TECH
    runtime_state = await get_chat_runtime(db, db.session_row)
    assert runtime_state["sessionStatus"] == "CLOSED"
    assert runtime_state["endReason"] == "FAST_FAIL_TECH"

    # Subsequent complete_chat_session call preserves FAST_FAIL_TECH (first-write-wins)
    complete_res = await complete_chat_session(db, db.session_row, reason="USER_ENDED")
    assert complete_res["sessionStatus"] == "CLOSED"
    assert complete_res["endReason"] == "FAST_FAIL_TECH"
    assert db.session_row["end_reason"] == "FAST_FAIL_TECH"

    second_meta = db.session_row["metadata"]
    if isinstance(second_meta, str):
        second_meta = json.loads(second_meta)
    assert second_meta["consecutive_uncooperative"] == 2
    assert second_meta["consecutive_fails"] == 2


@pytest.mark.asyncio
async def test_turn_transition_does_not_repeat_question(mock_session_data):
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    # Candidate provides a comprehensive answer to Turn 0 (RAG architecture)
    good_answer = (
        "Hệ thống RAG của mình gồm Embedding Model, Vector DB (Milvus) để truy xuất top-k chunk "
        "kết hợp Hybrid Search (BM25 + Dense Vector), sau đó dùng Cross-Encoder Reranker để rerank. "
        "Prompt cuối cùng đưa vào LLM kèm context giúp giảm thiểu hallucination xuống dưới 2%."
    )
    res = await process_candidate_message(
        db,
        session_row,
        client_message_id="msg-ans-1",
        content=good_answer,
    )
    assert res["sessionStatus"] == "OPEN"

    # If probed, answer the probe to advance turn
    if res["turnStatus"].get("isFollowUp"):
        res = await process_candidate_message(
            db,
            session_row,
            client_message_id="msg-ans-probe",
            content="Mình dùng Async Task Queue với RabbitMQ để retry khi Milvus chậm.",
        )
        assert res["sessionStatus"] == "OPEN"

    assert res["turnStatus"]["turnIndex"] == 1
    # Ensure next question is Turn 1 (Docker) and NOT repeating Turn 0 (RAG)
    assert "Docker" in res["assistantResponse"]["content"]
    assert "Trình bày về RAG architecture?" not in res["assistantResponse"]["content"]
    assert db.turns["turn-1"]["status"] == "ANSWERED"
    assert db.turns["turn-2"]["status"] == "ASKED"


@pytest.mark.asyncio
async def test_closing_reverse_qna_flow(mock_session_data):
    session_row, turns = mock_session_data
    turns[0]["question_snapshot"]["stage"] = "DEEP_DIVE"
    turns[1]["question_snapshot"] = {
        "stage": "BEHAVIORAL",
        "questionText": "Hãy kể về một lần bạn xử lý xung đột kỹ thuật với đồng nghiệp.",
        "target": {"conceptId": "behavioral-collaboration"},
    }
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    # 1. Candidate answers the only technical question well
    good_answer = (
        "Hệ thống RAG của mình gồm Embedding Model, Vector DB (Milvus) để truy xuất top-k chunk "
        "kết hợp Hybrid Search (BM25 + Dense Vector), sau đó dùng Cross-Encoder Reranker để rerank. "
        "Prompt cuối cùng đưa vào LLM kèm context giúp giảm thiểu hallucination xuống dưới 2%."
    )
    res1 = await process_candidate_message(
        db,
        session_row,
        client_message_id="msg-tech-done-1",
        content=good_answer,
    )
    assert res1["sessionStatus"] == "OPEN"

    # If probed, candidate answers the probe; the next frozen turn must be Behavioral.
    if res1["turnStatus"].get("isFollowUp"):
        res_after_technical = await process_candidate_message(
            db,
            session_row,
            client_message_id="msg-tech-done-2",
            content="Nếu Milvus timeout quá 500ms, hệ thống sẽ tự động fallback sang BM25 trên Elasticsearch và giảm số chunk rerank.",
        )
        assert res_after_technical["sessionStatus"] == "OPEN"
    else:
        res_after_technical = res1
    assert "xung đột kỹ thuật" in res_after_technical["assistantResponse"]["content"]

    # Behavioral must complete before Closing becomes available.
    res_behavioral = await process_candidate_message(
        db,
        session_row,
        client_message_id="msg-behavioral-done",
        content=(
            "Tôi chủ động trao đổi riêng với đồng nghiệp, cùng kiểm tra số liệu và thống nhất thử nghiệm hai phương án. "
            "Sau đó nhóm chọn giải pháp có latency tốt hơn và ghi lại quyết định để tránh lặp lại xung đột."
        ),
    )
    assert res_behavioral["sessionStatus"] == "OPEN"
    assert res_behavioral["currentStage"] == "CLOSING"
    assert res_behavioral["action"] == "ASK_MAIN"

    # 2. Candidate asks a question about INTERVIA tech stack
    res2 = await process_candidate_message(
        db,
        session_row,
        client_message_id="msg-qna-1",
        content="Cho mình hỏi dự án sắp tới của team AI sẽ sử dụng những công nghệ và framework nào vậy?",
    )
    assert res2["sessionStatus"] == "OPEN"
    assert res2["turnStatus"]["isFollowUp"] is True

    # 3. Candidate wraps up
    res3 = await process_candidate_message(
        db,
        session_row,
        client_message_id="msg-qna-done",
        content="Dạ mình nắm rõ rồi, mình không còn câu hỏi nào nữa. Cảm ơn bạn rất nhiều!",
    )
    assert res3["sessionStatus"] == "CLOSED"
    assert "Chúc mừng bạn đã hoàn thành" in res3["assistantResponse"]["content"] or "báo cáo đánh giá" in res3["assistantResponse"]["content"]
    assert db.session_row["status"] == "CLOSED"


# =============================================================================
# MANDATORY GATE 4 TEST MATRIX (RT-01 .. RT-14)
# =============================================================================

@pytest.mark.asyncio
async def test_rt01_happy_path(mock_session_data):
    """RT-01: Main question -> sufficient answer -> next frozen main question."""
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    good_answer = (
        "Hệ thống RAG của mình gồm Embedding Model, Vector DB (Milvus) để truy xuất top-k chunk "
        "kết hợp Hybrid Search (BM25 + Dense Vector), sau đó dùng Cross-Encoder Reranker để rerank. "
        "Prompt cuối cùng đưa vào LLM kèm context giúp giảm thiểu hallucination xuống dưới 2%."
    )
    res = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt01-msg-1",
        content=good_answer,
    )
    if res.get("action") == "PROBE" or res.get("turnStatus", {}).get("isFollowUp"):
        res = await process_candidate_message(
            db,
            session_row,
            client_message_id="rt01-msg-probe",
            content="Mình dùng Async Task Queue với RabbitMQ để retry khi Milvus chậm.",
        )

    assert res["sessionStatus"] == "OPEN"
    assert res["turnStatus"]["turnIndex"] == 1
    assert "Docker" in res["assistantResponse"]["content"]
    assert db.turns["turn-1"]["status"] == "ANSWERED"
    assert db.turns["turn-2"]["status"] == "ASKED"


@pytest.mark.asyncio
async def test_rt02_weak_answer_triggers_probe(mock_session_data):
    """RT-02: Insufficient evidence -> probe."""
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    weak_answer = "Em chỉ dùng thư viện có sẵn thôi, không nhớ rõ cấu trúc bên trong."
    res = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt02-msg-weak",
        content=weak_answer,
    )

    assert res["sessionStatus"] == "OPEN"
    assert res["action"] == "PROBE"
    assert res["turnStatus"]["isFollowUp"] is True
    assert res["probeCount"] == 1
    assert res["completed"] is False


@pytest.mark.asyncio
async def test_rt03_probe_answer_sufficient(mock_session_data):
    """RT-03: Probe answer fills missing evidence -> advance."""
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    # 1. Weak answer triggers probe
    res1 = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt03-msg-1",
        content="Em dùng RAG.",
    )
    assert res1["action"] == "PROBE"

    # 2. Detailed follow-up fulfills missing evidence
    res2 = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt03-msg-2",
        content=(
            "Chi tiết hơn, mình triển khai Semantic Chunking với kích thước 512 tokens, "
            "dùng embedding model text-embedding-3-small, lưu vào Milvus HNSW index. "
            "Khi truy vấn, kết hợp BM25 sparse vector và dense vector qua Reciprocal Rank Fusion, "
            "sau đó dùng BGE-Reranker-Large để chọn top 3 context đưa vào LLM."
        ),
    )

    assert res2["sessionStatus"] == "OPEN"
    assert res2["action"] == "ASK_MAIN"
    assert res2["turnStatus"]["turnIndex"] == 1
    assert db.turns["turn-1"]["status"] == "ANSWERED"
    assert db.turns["turn-2"]["status"] == "ASKED"


@pytest.mark.asyncio
async def test_rt04_local_one_probe_per_turn(mock_session_data):
    """RT-04: local per-turn probe limit advances after one probe; no session cap is used."""
    session_row, turns = mock_session_data
    session_row["metadata"] = {"max_runtime_probes": 0, "allow_early_exit": False}
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    # First weak answer -> allowed probe
    res1 = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt04-msg-1",
        content="Em chỉ làm qua thôi.",
    )
    assert res1["action"] == "PROBE"

    # The legacy metadata cap is ignored; the second answer advances only because
    # this same turn already consumed its one local probe.
    res2 = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt04-msg-2",
        content="Em không nhớ chi tiết thêm được.",
    )
    assert res2["action"] in ("ASK_MAIN", "ADVANCE")
    assert res2["turnStatus"]["turnIndex"] == 1
    assert db.turns["turn-1"]["status"] == "ANSWERED"


@pytest.mark.asyncio
async def test_rt05_clarification(mock_session_data):
    """RT-05: Ambiguous / clarification request -> clarify."""
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    res = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt05-msg-clarify",
        content="Anh có thể giải thích rõ hơn câu hỏi này được không ạ? Em chưa hiểu rõ ý anh.",
    )

    assert res["action"] == "CLARIFY"
    assert res["turnStatus"]["isFollowUp"] is True
    assert res["completed"] is False
    assert len(res["message"]) > 0


@pytest.mark.asyncio
async def test_rt06_cv_followup_quota(mock_session_data):
    """RT-06: First CV follow-up allowed, second blocked."""
    session_row, turns = mock_session_data
    # Configure turn 0 as VALIDATE stage
    turns[0]["question_snapshot"]["stage"] = "VALIDATE"
    session_row["metadata"] = {"allow_early_exit": False}
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    # 1st weak answer in VALIDATE -> first follow-up allowed
    res1 = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt06-msg-1",
        content="Em có làm dự án đó trong CV.",
    )
    assert res1["action"] == "PROBE"

    # 2nd weak answer in VALIDATE -> N_cv_followup_max=1 exceeded -> advance
    res2 = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt06-msg-2",
        content="Em chỉ tham gia một phần nhỏ thôi.",
    )
    assert res2["action"] in ("ASK_MAIN", "ADVANCE")
    assert res2["turnStatus"]["turnIndex"] == 1


@pytest.mark.asyncio
async def test_rt07_time_guardrail_behavioral_reserve(mock_session_data):
    """RT-07: Without Behavioral in queue, pacing continues assessment and never opens Closing."""
    session_row, turns = mock_session_data
    # Session 25 mins = 1500s. closing_reserve = 60s, behavioral_reserve = 180s. Total reserve = 240s.
    # If candidate answers with duration_seconds = 1300s -> remaining_time = 200s (< 240s).
    session_row["metadata"] = {
        "closing_reserve_seconds": 60,
        "behavioral_reserve_seconds": 180,
    }
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    res = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt07-msg-reserve",
        content="Em chỉ làm cơ bản thôi ạ.",
        duration_seconds=1300.0,
    )
    # Behind-schedule pacing blocks the probe, but absence of Behavioral must not
    # create a Behavioral or Closing turn.
    assert res["action"] in ("ASK_MAIN", "ADVANCE", "COMPLETE")
    assert res["action"] != "PROBE"
    assert res["action"] != "ASK_CLOSING"


@pytest.mark.asyncio
async def test_rt08_time_guardrail_closing_reserve(mock_session_data):
    """RT-08: The 90-second cutoff closes safely; it never opens Closing from Technical."""
    session_row, turns = mock_session_data
    # duration_seconds = 1460s -> remaining_time = 40s (<= 60s closing reserve)
    session_row["metadata"] = {
        "closing_reserve_seconds": 60,
    }
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    res = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt08-msg-closing",
        content="Em có nắm được một chút.",
        duration_seconds=1460.0,
    )
    assert res["action"] == "COMPLETE"
    assert res["sessionStatus"] == "CLOSED"
    assert res["assistantResponse"]["messageType"] == "WRAP_UP"


@pytest.mark.asyncio
async def test_rt09_reverse_qna(mock_session_data):
    """RT-09: Behavioral completes before Closing reverse Q&A starts."""
    session_row, turns = mock_session_data
    turns[0]["question_snapshot"]["stage"] = "DEEP_DIVE"
    turns[1]["question_snapshot"] = {
        "stage": "BEHAVIORAL",
        "questionText": "Hãy kể về một lần bạn xử lý xung đột kỹ thuật với đồng nghiệp.",
        "target": {"conceptId": "behavioral-collaboration"},
    }
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    # 1. Candidate answers the only technical question well
    good_answer = (
        "Hệ thống RAG của mình gồm Embedding Model, Vector DB (Milvus) để truy xuất top-k chunk "
        "kết hợp Hybrid Search (BM25 + Dense Vector), sau đó dùng Cross-Encoder Reranker để rerank. "
        "Prompt cuối cùng đưa vào LLM kèm context giúp giảm thiểu hallucination xuống dưới 2%."
    )
    res1 = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt09-msg-1",
        content=good_answer,
    )
    if res1["turnStatus"].get("isFollowUp"):
        res_after_technical = await process_candidate_message(
            db,
            session_row,
            client_message_id="rt09-msg-probe",
            content="Nếu Milvus timeout quá 500ms, hệ thống sẽ tự động fallback sang BM25 trên Elasticsearch và giảm số chunk rerank.",
        )
    else:
        res_after_technical = res1
    assert "xung đột kỹ thuật" in res_after_technical["assistantResponse"]["content"]

    res_behavioral = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt09-behavioral",
        content=(
            "Tôi trao đổi riêng với đồng nghiệp, cùng kiểm tra số liệu và thử nghiệm hai phương án. "
            "Nhóm chọn giải pháp có latency tốt hơn và ghi lại quyết định kỹ thuật."
        ),
    )
    assert res_behavioral["currentStage"] == "CLOSING"

    # Candidate asks reverse question only after Behavioral has completed.
    res_qna = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt09-qna",
        content="Cho em hỏi team mình đang ứng dụng AI vào những bài toán thực tế nào?",
    )
    assert res_qna["sessionStatus"] == "OPEN"

    # 3. Candidate wraps up
    res_end = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt09-done",
        content="Dạ em cảm ơn anh, em đã nắm rõ thông tin rồi ạ.",
    )
    assert res_end["sessionStatus"] == "CLOSED"
    assert res_end["completed"] is True


@pytest.mark.asyncio
async def test_rt10_duplicate_client_message_id(mock_session_data):
    """RT-10: Same client_message_id twice -> exactly-once semantic effect."""
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    with patch("src.modules.interviews.application.chat_runtime.generate_text", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = '{"decision": "PROBE", "reply_text": "Bạn tối ưu latency như thế nào?"}'

        res1 = await process_candidate_message(
            db,
            session_row,
            client_message_id="rt10-idemp-1",
            content="Đây là câu trả lời kiểm tra tính bất biến.",
        )

        count_after_first = len(db.messages)

        res2 = await process_candidate_message(
            db,
            session_row,
            client_message_id="rt10-idemp-1",
            content="Đây là câu trả lời kiểm tra tính bất biến gửi lại.",
        )

        # No duplicate messages inserted
        assert len(db.messages) == count_after_first
        assert res1["userMessage"]["messageId"] == res2["userMessage"]["messageId"]
        if res1.get("assistantResponse") and res2.get("assistantResponse"):
            assert res1["assistantResponse"]["messageId"] == res2["assistantResponse"]["messageId"]


@pytest.mark.asyncio
async def test_rt11_no_repeated_main_question(mock_session_data):
    """RT-11: Completed frozen turn -> never selected again."""
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    good_answer = (
        "Hệ thống RAG của mình gồm Embedding Model, Vector DB (Milvus) để truy xuất top-k chunk "
        "kết hợp Hybrid Search (BM25 + Dense Vector), sau đó dùng Cross-Encoder Reranker để rerank. "
        "Prompt cuối cùng đưa vào LLM kèm context giúp giảm thiểu hallucination xuống dưới 2%."
    )
    res = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt11-msg-1",
        content=good_answer,
    )
    if res.get("action") == "PROBE" or res.get("turnStatus", {}).get("isFollowUp"):
        res = await process_candidate_message(
            db,
            session_row,
            client_message_id="rt11-msg-probe",
            content="Mình dùng Async Task Queue với RabbitMQ để retry khi Milvus chậm và Elasticsearch làm BM25 fallback.",
        )

    # Next question is Turn 1 (Docker), Turn 0 (RAG) is NEVER repeated
    assert res["turnStatus"]["turnIndex"] == 1
    assert "Docker" in res["assistantResponse"]["content"]
    assert "Trình bày về RAG architecture?" not in res["assistantResponse"]["content"]


@pytest.mark.asyncio
async def test_rt12_illegal_lifecycle_fail_fast():
    """RT-12: Session invalid/not ready/completed -> fail-fast."""
    # 1. P2 plan not locked
    session_not_locked = {"id": "s-draft", "status": "OPEN", "plan_status": "DRAFT"}
    db1 = MockChatSession(session_not_locked, [])
    with pytest.raises(ChatRuntimeError, match="P2_NOT_LOCKED"):
        await start_chat_session(db1, session_not_locked)

    with pytest.raises(ChatRuntimeError, match="P2_NOT_LOCKED"):
        await process_candidate_message(db1, session_not_locked, client_message_id="m1", content="Hi")

    # 2. Session already closed
    session_closed = {"id": "s-closed", "status": "CLOSED", "plan_status": "LOCKED"}
    db2 = MockChatSession(session_closed, [])
    with pytest.raises(ChatRuntimeError, match="ILLEGAL_SESSION_STATE"):
        await process_candidate_message(db2, session_closed, client_message_id="m2", content="Hi")

    # 3. No turns found
    session_no_turns = {"id": "s-no-turns", "status": "OPEN", "plan_status": "LOCKED"}
    db3 = MockChatSession(session_no_turns, [])
    with pytest.raises(ChatRuntimeError, match="QUESTION_SNAPSHOT_MISSING"):
        await start_chat_session(db3, session_no_turns)


@pytest.mark.asyncio
async def test_rt13_reload_resume(mock_session_data):
    """RT-13: Restart runtime -> restores same current turn and working memory state."""
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    # Turn 0 probed
    res1 = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt13-msg-1",
        content="Em chỉ biết sơ qua về RAG.",
    )
    assert res1["action"] == "PROBE"

    # Simulate reload by querying get_chat_runtime
    runtime_state = await get_chat_runtime(db, session_row)

    assert runtime_state["currentTurnIndex"] == 0
    assert runtime_state["currentTurn"]["turnIndex"] == 0
    assert runtime_state["currentTurn"]["isFollowUp"] is True
    assert runtime_state["currentTurn"]["probeCount"] == 1
    assert "workingMemory" in runtime_state
    wm = runtime_state["workingMemory"]
    assert wm["probe_count"] == 1
    assert "candidate_context" in wm
    assert "current_stage" in wm


@pytest.mark.asyncio
async def test_rt14_completion_idempotency(mock_session_data):
    """RT-14: Complete twice -> no duplicate side effects."""
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    res1 = await complete_chat_session(db, session_row, reason="USER_ENDED")
    assert res1["sessionStatus"] == "CLOSED"
    msg_count_1 = len(db.messages)

    # Second completion call
    res2 = await complete_chat_session(db, session_row, reason="USER_ENDED")
    assert res2["sessionStatus"] == "CLOSED"
    assert len(db.messages) == msg_count_1  # No duplicate WRAP_UP message inserted


@pytest.mark.asyncio
async def test_rt15_candidate_message_when_p2_ready(mock_session_data):
    """RT-15: Candidate message when P2 READY -> P2_NOT_LOCKED, no writes."""
    session_row, turns = mock_session_data
    session_row["plan_status"] = "READY"
    db = MockChatSession(session_row, turns)

    initial_msg_count = len(db.messages)
    current_turn_id = turns[0]["id"]
    initial_answer_text = db.turns[current_turn_id].get("answer_text")

    # plan_status = READY -> ChatRuntimeError(P2_NOT_LOCKED)
    with pytest.raises(ChatRuntimeError, match="P2_NOT_LOCKED"):
        await process_candidate_message(
            db,
            session_row,
            client_message_id="rt15-msg-1",
            content="Xin chào tôi muốn trả lời câu hỏi.",
        )

    # Invariants:
    # 1. No candidate message inserted
    assert len(db.messages) == initial_msg_count
    # 2. No answer_text update
    assert db.turns[current_turn_id].get("answer_text") == initial_answer_text
    # 3. Session status unchanged
    assert db.session_row.get("status") == "OPEN"

    # Also verify DRAFT, None, and invalid statuses fail-fast with P2_NOT_LOCKED
    for invalid_status in ("DRAFT", None, "INVALID_STATUS"):
        session_row["plan_status"] = invalid_status
        with pytest.raises(ChatRuntimeError, match="P2_NOT_LOCKED"):
            await process_candidate_message(
                db,
                session_row,
                client_message_id=f"rt15-msg-{invalid_status}",
                content="Câu trả lời thử nghiệm",
            )


@pytest.mark.asyncio
async def test_rt16_score_cannot_terminate_session(mock_session_data):
    """RT-16: Score has zero control-flow authority; decisions rely strictly on sufficiency semantics."""
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    # 1. Low numeric score (score=1.0) but semantically SUFFICIENT
    low_score_sufficient_eval = {
        "score": 1.0,
        "is_sufficient": True,
        "sufficiency_status": "SUFFICIENT",
        "intent": "ANSWER",
        "acknowledgement": "Ghi nhận giải pháp của bạn.",
    }

    with patch.object(InterviewCoreEngine, "_evaluate_candidate_response", new_callable=AsyncMock) as mock_eval:
        mock_eval.return_value = low_score_sufficient_eval

        res1 = await process_candidate_message(
            db,
            session_row,
            client_message_id="rt16-low-score-1",
            content="Đây là câu trả lời kiến trúc hoàn chỉnh đáp ứng đầy đủ yêu cầu.",
        )

        # Expected: Runtime follows sufficiency semantics, NOT numeric score.
        # Must NOT terminate session just because score is 1.0.
        assert res1["sessionStatus"] == "OPEN"
        assert res1["completed"] is False
        assert res1["action"] == "ASK_MAIN"
        assert res1["turnStatus"]["turnIndex"] == 1

    # 2. Reverse test: High numeric score (score=10.0) but semantically INSUFFICIENT
    high_score_insufficient_eval = {
        "score": 10.0,
        "is_sufficient": False,
        "sufficiency_status": "INSUFFICIENT",
        "intent": "ANSWER",
        "missing_aspect": "Thiếu phân tích throughput và latency",
        "acknowledgement": "Cảm ơn câu trả lời của bạn.",
    }

    with patch.object(InterviewCoreEngine, "_evaluate_candidate_response", new_callable=AsyncMock) as mock_eval:
        mock_eval.return_value = high_score_insufficient_eval

        res2 = await process_candidate_message(
            db,
            session_row,
            client_message_id="rt16-high-score-probe",
            content="Tôi dùng Docker container để đóng gói ứng dụng.",
        )

        # Expected: Probe triggered according to sufficiency + quota.
        # High numeric score cannot override insufficiency semantics.
        assert res2["sessionStatus"] == "OPEN"
        assert res2["completed"] is False
        assert res2["action"] == "PROBE"
        assert res2["turnStatus"]["isFollowUp"] is True


@pytest.mark.asyncio
async def test_rt17_hard_timeout_persistence(mock_session_data):
    """RT-17: Hard timeout preserves HARD_TIMEOUT endReason across session and reloads."""
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    # Simulate turn input when duration_seconds exceeds/reaches hard timeout (time_remaining <= 30s)
    target_duration = int(session_row.get("duration_minutes", 25)) * 60
    near_expiry_duration = target_duration - 15  # remaining 15 seconds <= 30s hard timeout

    res = await process_candidate_message(
        db,
        session_row,
        client_message_id="rt17-timeout-msg",
        content="Câu trả lời cuối cùng trước khi hết giờ.",
        duration_seconds=near_expiry_duration,
    )

    # 1. Session is CLOSED with HARD_TIMEOUT
    assert res["sessionStatus"] == "CLOSED"
    assert res["completed"] is True
    assert res["endReason"] == "HARD_TIMEOUT"
    assert res["action"] == "COMPLETE"
    assert db.session_row["status"] == "CLOSED"
    assert db.session_row["end_reason"] == "HARD_TIMEOUT"

    # Exactly one WRAP_UP message inserted
    wrap_ups = [m for m in db.messages if m.get("message_type") == "WRAP_UP"]
    assert len(wrap_ups) == 1

    # 2. Reload via get_chat_runtime returns persisted HARD_TIMEOUT
    runtime_state = await get_chat_runtime(db, db.session_row)
    assert runtime_state["sessionStatus"] == "CLOSED"
    assert runtime_state["endReason"] == "HARD_TIMEOUT"

    # 3. Subsequent complete_chat_session call preserves HARD_TIMEOUT (first-write-wins)
    complete_res = await complete_chat_session(db, db.session_row, reason="USER_ENDED")
    assert complete_res["sessionStatus"] == "CLOSED"
    assert complete_res["endReason"] == "HARD_TIMEOUT"
    assert db.session_row["end_reason"] == "HARD_TIMEOUT"


@pytest.mark.asyncio
async def test_rt18_reserve_precedence_over_elapsed_ratio_heuristic():
    """RT-18: cutoff and valid Behavioral reserve precedence follow the Gate 4 contract."""
    from src.modules.interviews.core.interview_types import InterviewStage

    engine = InterviewCoreEngine()

    deep_dive_q = {
        "question_id": "q-dd-1",
        "question_version_id": "qv-dd-1",
        "stage": InterviewStage.DEEP_DIVE,
        "competency": "Kỹ thuật chuyên sâu",
        "main_prompt": "Thiết kế kiến trúc sharding cho PostgreSQL",
    }
    beh_q = {
        "question_id": "q-beh-1",
        "question_version_id": "qv-beh-1",
        "stage": InterviewStage.BEHAVIORAL,
        "competency": "Hành vi ứng xử",
        "main_prompt": "Kể lại một lần bạn xung đột kỹ thuật với đồng nghiệp",
    }

    # Case 1: DEEP_DIVE stage with elapsed ratio 0.60 (< 0.75 ratio threshold).
    # Remaining time = 45s <= the 90-second cutoff. No Closing may open.
    session_state_closing = {
        "current_stage": InterviewStage.DEEP_DIVE.value,
        "target_duration_minutes": 25,
        "elapsed_time": 900,  # 900 / 1500 = 0.60 < 0.75
        "remaining_time": 45,  # <= 60s closing reserve
        "closing_reserve_seconds": 60,
        "behavioral_reserve_seconds": 180,
        "questions_pool": {
            InterviewStage.DEEP_DIVE.value: [deep_dive_q],
            InterviewStage.BEHAVIORAL.value: [beh_q],
        },
        "asked_question_ids": [],
    }

    stage, q = engine._get_next_stage_and_question(session_state_closing)
    assert stage == InterviewStage.CLOSED
    assert q is None

    # Case 2: DEEP_DIVE stage with elapsed ratio 0.60 (< 0.75).
    # Remaining time = 200s <= (closing_reserve 60s + behavioral_reserve 180s = 240s).
    # Behavioral Reserve MUST WIN over 75% heuristic!
    session_state_behavioral = {
        "current_stage": InterviewStage.DEEP_DIVE.value,
        "target_duration_minutes": 25,
        "elapsed_time": 900,
        "remaining_time": 200,  # <= 240s
        "closing_reserve_seconds": 60,
        "behavioral_reserve_seconds": 180,
        "questions_pool": {
            InterviewStage.DEEP_DIVE.value: [deep_dive_q],
            InterviewStage.BEHAVIORAL.value: [beh_q],
        },
        "asked_question_ids": [],
    }

    stage_beh, q_beh = engine._get_next_stage_and_question(session_state_behavioral)
    assert stage_beh == InterviewStage.BEHAVIORAL
    assert q_beh is not None
    assert q_beh.question_id == "q-beh-1"

    # Case 3: the same reserve without a Behavioral turn keeps the assessment queue.
    session_state_no_behavioral = {
        **session_state_behavioral,
        "questions_pool": {InterviewStage.DEEP_DIVE.value: [deep_dive_q]},
    }
    stage_no_beh, q_no_beh = engine._get_next_stage_and_question(session_state_no_behavioral)
    assert stage_no_beh == InterviewStage.DEEP_DIVE
    assert q_no_beh is not None
    assert q_no_beh.question_id == "q-dd-1"


@pytest.mark.asyncio
async def test_rt19_concurrent_duplicate_client_message(mock_session_data):
    """RT-19: Concurrent requests with same client_message_id -> safe duplicate-race recovery, exactly-once semantics."""
    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    cid = "concurrent-idemp-uuid-1"
    content = "Câu trả lời gửi đồng thời từ client."

    # First request proceeds normally and commits user message + assistant reply
    res1 = await process_candidate_message(
        db,
        session_row,
        client_message_id=cid,
        content=content,
    )

    count_after_first = len(db.messages)
    user_msg_id = res1["userMessage"]["messageId"]
    asst_msg_id = res1["assistantResponse"]["messageId"]

    # Second concurrent request arrives with same client_message_id.
    # In MockChatSession, inserting a message with an already-existing client_message_id
    # simulates the PostgreSQL unique constraint collision and raises IntegrityError.
    # Runtime catches IntegrityError, performs rollback, fetches existing response, and returns it.
    res2 = await process_candidate_message(
        db,
        session_row,
        client_message_id=cid,
        content=content,
    )

    # Invariants:
    # 1. Exactly one candidate message with this client_message_id
    cid_messages = [m for m in db.messages if m.get("client_message_id") == cid]
    assert len(cid_messages) == 1

    # 2. Total messages in DB unchanged (no duplicate insertion)
    assert len(db.messages) == count_after_first

    # 3. Identical response messages returned to both callers
    assert res2["userMessage"]["messageId"] == user_msg_id
    assert res2["assistantResponse"]["messageId"] == asst_msg_id

    # 4. Exactly one turn transition
    assert res1["currentTurnIndex"] == res2["currentTurnIndex"]
    assert res1["completed"] == res2["completed"]


@pytest.mark.asyncio
async def test_fast_fail_tech_end_reason_contract(mock_session_data):
    """Confirm FAST_FAIL_TECH is emitted upon 2 explicit Give Ups in Technical stage,
    persisted as FAST_FAIL_TECH (not COMPLETED), retained on reload, and preserved
    against subsequent complete calls."""
    from src.modules.interviews.api.router import CompleteChatSession

    session_row, turns = mock_session_data
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    # 1. First Give Up in Technical stage
    res1 = await process_candidate_message(
        db,
        session_row,
        client_message_id="giveup-tech-1",
        content="em không biết",
    )
    assert res1["sessionStatus"] == "OPEN"
    assert res1["assistantResponse"]["messageType"] == "MAIN_QUESTION"

    # 2. Second Give Up in Technical stage triggers FAST_FAIL_TECH
    res2 = await process_candidate_message(
        db,
        session_row,
        client_message_id="giveup-tech-2",
        content="em chịu",
    )
    assert res2["sessionStatus"] == "CLOSED"
    assert res2["completed"] is True
    assert res2["endReason"] == "FAST_FAIL_TECH"
    assert res2["action"] == "COMPLETE"
    assert db.session_row["status"] == "CLOSED"
    assert db.session_row["end_reason"] == "FAST_FAIL_TECH"

    # 3. Reload via get_chat_runtime returns FAST_FAIL_TECH
    reloaded = await get_chat_runtime(db, db.session_row)
    assert reloaded["sessionStatus"] == "CLOSED"
    assert reloaded["endReason"] == "FAST_FAIL_TECH"

    # 4. Subsequent complete_chat_session preserves existing FAST_FAIL_TECH
    comp_res = await complete_chat_session(db, db.session_row, reason="USER_ENDED")
    assert comp_res["sessionStatus"] == "CLOSED"
    assert comp_res["endReason"] == "FAST_FAIL_TECH"
    assert db.session_row["end_reason"] == "FAST_FAIL_TECH"

    # 5. Client completion request model: allows user-driven reasons, defaults to USER_ENDED
    assert CompleteChatSession().reason == "USER_ENDED"
    for client_reason in ["COMPLETED", "USER_ENDED", "TECHNICAL_FAILURE"]:
        m = CompleteChatSession(reason=client_reason)
        assert m.reason == client_reason

    # 6. Client completion request model: REJECTS system/runtime-determined reasons
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        CompleteChatSession(reason="FAST_FAIL_TECH")

    with pytest.raises(pydantic.ValidationError):
        CompleteChatSession(reason="HARD_TIMEOUT")


def test_migration_0020_downgrade_remap_lossy_contract():
    """Verify migration 20261002_0020 explicitly documents lossy downgrade
    remap and satisfies the legacy constraint without claiming semantic preservation."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "migration_0020",
        "d:/KLTN/interview-prep-core/migrations/versions/20261002_0020_allow_fast_fail_tech_end_reason.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.revision == "20261002_0020"
    assert mod.down_revision == "20260930_0019"
    # Inspect docstring acknowledging lossy fallback
    assert "KHÔNG THỂ giữ nguyên mã 'FAST_FAIL_TECH'" in mod.__doc__
    assert "TUYỆT ĐỐI KHÔNG BẢO TOÀN NGỮ NGHĨA" in mod.__doc__


# =============================================================================
# GATE 4 FSM DEFECT REGRESSION TESTS
# =============================================================================

@pytest.mark.asyncio
async def test_regression_multi_target_queue_drains_deep_dive_after_challenge():
    """Requirement 1: Multi-target queue must complete all pending DEEP_DIVE
    and CHALLENGE turns across all competencies before transitioning to BEHAVIORAL."""
    session_row = {
        "id": "sess-multi-target",
        "user_id": "usr-1",
        "job_id": "job-1",
        "status": "OPEN",
        "plan_status": "LOCKED",
        "duration_minutes": 25,
        "started_at": datetime.now(UTC),
    }
    turns = [
        {
            "id": "t0",
            "session_id": "sess-multi-target",
            "turn_index": 0,
            "status": "PLANNED",
            "question_version_id": "qv0",
            "question_snapshot": {"stage": "WARM_UP", "questionText": "Warmup prompt", "target": {"conceptId": "intro"}},
        },
        {
            "id": "t1",
            "session_id": "sess-multi-target",
            "turn_index": 1,
            "status": "PLANNED",
            "question_version_id": "qv1",
            "question_snapshot": {"stage": "VALIDATE", "questionText": "Validate prompt", "target": {"conceptId": "val"}},
        },
        {
            "id": "t2",
            "session_id": "sess-multi-target",
            "turn_index": 2,
            "status": "PLANNED",
            "question_version_id": "qv2",
            "question_snapshot": {"stage": "DEEP_DIVE", "questionText": "Deep Dive AI", "taxonomyTarget": {"label": "AI"}},
        },
        {
            "id": "t3",
            "session_id": "sess-multi-target",
            "turn_index": 3,
            "status": "PLANNED",
            "question_version_id": "qv3",
            "question_snapshot": {"stage": "CHALLENGE", "questionText": "Challenge AI", "taxonomyTarget": {"label": "AI"}},
        },
        {
            "id": "t4",
            "session_id": "sess-multi-target",
            "turn_index": 4,
            "status": "PLANNED",
            "question_version_id": "qv4",
            "question_snapshot": {"stage": "DEEP_DIVE", "questionText": "Deep Dive GenAI 1", "taxonomyTarget": {"label": "GenAI"}},
        },
        {
            "id": "t5",
            "session_id": "sess-multi-target",
            "turn_index": 5,
            "status": "PLANNED",
            "question_version_id": "qv5",
            "question_snapshot": {"stage": "DEEP_DIVE", "questionText": "Deep Dive GenAI 2", "taxonomyTarget": {"label": "GenAI"}},
        },
        {
            "id": "t6",
            "session_id": "sess-multi-target",
            "turn_index": 6,
            "status": "PLANNED",
            "question_version_id": "qv6",
            "question_snapshot": {"stage": "BEHAVIORAL", "questionText": "Behavioral Teamwork", "taxonomyTarget": {"label": "Behavioral"}},
        },
    ]
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)
    assert db.turns["t0"]["status"] == "ASKED"

    with patch("src.modules.interviews.application.chat_runtime.generate_text", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = '{"intent": "ANSWER", "sufficiency_status": "SUFFICIENT", "acknowledgement": "OK"}'

        # Answer T0 -> advances to T1 (VALIDATE)
        r0 = await process_candidate_message(db, session_row, client_message_id="m0", content="Hi, my name is Candidate, and I am happy to be here for this technical interview session today.")
        assert r0["currentTurnIndex"] == 1
        assert db.turns["t0"]["status"] == "ANSWERED"
        assert db.turns["t1"]["status"] == "ASKED"

        # Answer T1 -> advances to T2 (DEEP_DIVE AI) - first item in DEEP_DIVE pool
        r1 = await process_candidate_message(db, session_row, client_message_id="m1", content="I have three years of hands-on experience in machine learning, working on NLP and recommendation systems using PyTorch and Scikit-learn.")
        assert r1["currentTurnIndex"] == 2
        assert db.turns["t1"]["status"] == "ANSWERED"
        assert db.turns["t2"]["status"] == "ASKED"
        assert db.turns["t6"]["status"] == "PLANNED", "BEHAVIORAL must not be activated while DEEP_DIVE/CHALLENGE pending"

        # Answer T2 -> engine drains DEEP_DIVE pool next (T4 GenAI), NOT CHALLENGE yet
        # Engine priority: DEEP_DIVE pool fully drained before CHALLENGE pool
        r2 = await process_candidate_message(db, session_row, client_message_id="m2", content="In my RAG pipeline I used FAISS as the vector store combined with hybrid BM25 search and a cross-encoder for reranking retrieved passages before generation.")
        next_idx_after_t2 = r2["currentTurnIndex"]
        assert db.turns["t2"]["status"] == "ANSWERED"
        assert db.turns["t6"]["status"] == "PLANNED", "BEHAVIORAL must not be activated while DEEP_DIVE/CHALLENGE pending"
        # Engine should advance to another DEEP_DIVE or CHALLENGE turn (T3/T4/T5), never to T6
        assert next_idx_after_t2 in (3, 4, 5), f"Expected technical turn (3,4,5) after T2 but got {next_idx_after_t2}"

        # Simulate answering all remaining technical turns until T6 is reached
        # Track which turns are still PLANNED and simulate answering them in order
        technical_turn_ids = ["t3", "t4", "t5"]
        msg_id = 3
        last_r = r2
        answered_technical = []
        for _ in range(len(technical_turn_ids)):
            current_idx = last_r["currentTurnIndex"]
            current_turn_id = next((tid for tid, t in db.turns.items() if t["turn_index"] == current_idx), None)
            if current_turn_id is None or db.turns.get(current_turn_id, {}).get("status") != "ASKED":
                break
            current_stage = last_r.get("currentStage", "")
            # Behavioral must not be reached while technical turns remain PLANNED
            assert current_stage != "BEHAVIORAL" or not any(
                db.turns[tid]["status"] == "PLANNED" for tid in technical_turn_ids
            ), f"BEHAVIORAL started at turn {current_idx} while technical turns still PLANNED"
            r = await process_candidate_message(
                db, session_row, client_message_id=f"m{msg_id}",
                content="Our solution achieved this by combining async IO patterns with batched vector retrieval and a lightweight reranker to minimize end-to-end latency under high concurrency workloads."
            )
            answered_technical.append(current_turn_id)
            last_r = r
            msg_id += 1

        # Final assertion: T6 BEHAVIORAL is now ASKED
        assert db.turns["t6"]["status"] == "ASKED", f"T6 BEHAVIORAL should be ASKED after all technical turns complete. Turns: {[(tid, db.turns[tid]['status']) for tid in ['t2','t3','t4','t5','t6']]}"
        assert last_r["currentStage"] == "BEHAVIORAL"
        # All technical turns should be ANSWERED
        for tid in technical_turn_ids:
            assert db.turns[tid]["status"] == "ANSWERED", f"{tid} should be ANSWERED but is {db.turns[tid]['status']}"


@pytest.mark.asyncio
async def test_regression_candidate_reply_binds_to_active_asked_turn_not_planned():
    """Requirement 2: Candidate reply binds to active ASKED turn, never to a lower PLANNED turn."""
    session_row = {
        "id": "sess-anom",
        "user_id": "usr-1",
        "job_id": "job-1",
        "status": "OPEN",
        "plan_status": "LOCKED",
    }
    turns = [
        {
            "id": "t-asked",
            "session_id": "sess-anom",
            "turn_index": 2,
            "status": "ASKED",
            "question_snapshot": {"stage": "DEEP_DIVE", "questionText": "Active question"},
        },
        {
            "id": "t-planned",
            "session_id": "sess-anom",
            "turn_index": 1,
            "status": "PLANNED",
            "question_snapshot": {"stage": "VALIDATE", "questionText": "Older planned question"},
        },
    ]
    db = MockChatSession(session_row, turns)
    db.messages.append({
        "id": "asst-msg-1",
        "session_id": "sess-anom",
        "role": "assistant",
        "content": "Active question",
        "turn_id": "t-asked",
        "message_type": "MAIN_QUESTION",
        "sequence": 1,
        "created_at": datetime.now(UTC),
    })

    with patch("src.modules.interviews.application.chat_runtime.generate_text", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = '{"intent": "ANSWER", "sufficiency_status": "SUFFICIENT"}'
        res = await process_candidate_message(
            db,
            session_row,
            client_message_id="msg-1",
            content="This is my detailed answer to the active question. I used a distributed caching layer with Redis and Kafka to decouple the read and write paths, reducing p99 latency significantly.",
        )
        assert res["userMessage"]["turnId"] == "t-asked"
        assert res["turnStatus"]["turnId"] == "t-asked"
        assert db.turns["t-asked"]["status"] == "ANSWERED"
        assert db.turns["t-planned"]["status"] == "PLANNED"


@pytest.mark.asyncio
async def test_regression_exactly_one_asked_turn_invariant():
    """Requirement 3: Exactly one ASKED turn allowed. Fails safely on multiple or zero ASKED."""
    session_row = {"id": "sess-inv", "status": "OPEN", "plan_status": "LOCKED"}

    # Case A: Multiple ASKED turns
    turns_multi = [
        {"id": "t1", "session_id": "sess-inv", "turn_index": 0, "status": "ASKED", "question_snapshot": {"stage": "WARM_UP"}},
        {"id": "t2", "session_id": "sess-inv", "turn_index": 1, "status": "ASKED", "question_snapshot": {"stage": "VALIDATE"}},
    ]
    db_multi = MockChatSession(session_row, turns_multi)
    with pytest.raises(ChatRuntimeError, match="đồng thời"):
        await process_candidate_message(db_multi, session_row, client_message_id="m1", content="Hi")

    # Case B: Zero ASKED turns with PLANNED turns remaining
    turns_zero = [
        {"id": "t1", "session_id": "sess-inv", "turn_index": 0, "status": "PLANNED", "question_snapshot": {"stage": "WARM_UP"}},
    ]
    db_zero = MockChatSession(session_row, turns_zero)
    with pytest.raises(ChatRuntimeError, match="Không có lượt câu hỏi nào đang ở trạng thái ASKED"):
        await process_candidate_message(db_zero, session_row, client_message_id="m1", content="Hi")

    # Case C: Behavioral ASKED while technical is still PLANNED
    turns_corrupted = [
        {"id": "t-tech", "session_id": "sess-inv", "turn_index": 0, "status": "PLANNED", "question_snapshot": {"stage": "DEEP_DIVE"}},
        {"id": "t-beh", "session_id": "sess-inv", "turn_index": 1, "status": "ASKED", "question_snapshot": {"stage": "BEHAVIORAL"}},
    ]
    db_corrupted = MockChatSession(session_row, turns_corrupted)
    db_corrupted.messages.append({
        "id": "asst-msg",
        "session_id": "sess-inv",
        "role": "assistant",
        "content": "Tell me about conflict",
        "turn_id": "t-beh",
        "sequence": 1,
        "message_type": "MAIN_QUESTION",
        "created_at": datetime.now(UTC),
    })
    with pytest.raises(ChatRuntimeError, match="khi còn câu hỏi kỹ thuật chưa hoàn thành"):
        await process_candidate_message(db_corrupted, session_row, client_message_id="m1", content="Hi")


@pytest.mark.asyncio
async def test_regression_cannot_complete_session_if_behavioral_unanswered():
    """Requirement 4: Behavioral not ANSWERED cannot complete session with COMPLETED."""
    session_row = {"id": "sess-beh-unanswered", "status": "OPEN", "plan_status": "LOCKED"}
    turns = [
        {
            "id": "t-tech",
            "session_id": "sess-beh-unanswered",
            "turn_index": 0,
            "status": "ANSWERED",
            "question_snapshot": {"stage": "DEEP_DIVE"},
        },
        {
            "id": "t-beh",
            "session_id": "sess-beh-unanswered",
            "turn_index": 1,
            "status": "PLANNED",
            "question_snapshot": {"stage": "BEHAVIORAL"},
        },
    ]
    db = MockChatSession(session_row, turns)
    with pytest.raises(ChatRuntimeError, match="Behavioral chưa hoàn tất"):
        await complete_chat_session(db, session_row, reason="COMPLETED")

    # But USER_ENDED is allowed (candidate voluntary abort)
    res_abort = await complete_chat_session(db, session_row, reason="USER_ENDED")
    assert res_abort["sessionStatus"] == "CLOSED"
    assert res_abort["endReason"] == "USER_ENDED"


@pytest.mark.asyncio
async def test_regression_behavioral_answered_transitions_to_closing():
    """Requirement 5: Behavioral answered transitions to CLOSING Q&A invitation,
    and then wraps up cleanly when candidate has no further questions."""
    session_row = {
        "id": "sess-closing-flow",
        "user_id": "usr-1",
        "job_id": "job-1",
        "status": "OPEN",
        "plan_status": "LOCKED",
        "duration_minutes": 25,
        "started_at": datetime.now(UTC),
    }
    turns = [
        {
            "id": "t-beh",
            "session_id": "sess-closing-flow",
            "turn_index": 0,
            "status": "PLANNED",
            "question_snapshot": {"stage": "BEHAVIORAL", "questionText": "Tell me about a conflict."},
        },
    ]
    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)
    assert db.turns["t-beh"]["status"] == "ASKED"

    with patch("src.modules.interviews.application.chat_runtime.generate_text", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = '{"intent": "ANSWER", "sufficiency_status": "SUFFICIENT"}'
        # Answer Behavioral -> transitions to CLOSING
        res_beh = await process_candidate_message(
            db,
            session_row,
            client_message_id="msg-beh",
            content="I resolved the conflict by aligning on data metrics.",
            duration_seconds=300,  # 25m - 5m = 20m remaining >= 180s
        )
        assert res_beh["currentStage"] == "CLOSING"
        assert res_beh["action"] == "ASK_MAIN"
        assert db.turns["t-beh"]["status"] == "ANSWERED"

        # In CLOSING: candidate asks questions
        mock_llm.return_value = "Tại INTERVIA chúng tôi áp dụng CI/CD hiện đại."
        res_qna = await process_candidate_message(
            db,
            session_row,
            client_message_id="msg-qna-1",
            content="Công ty áp dụng quy trình CI/CD như thế nào?",
            duration_seconds=360,
        )
        assert res_qna["currentStage"] == "CLOSING"
        assert res_qna["sessionStatus"] == "OPEN"

        # In CLOSING: candidate has no further questions -> wraps up session
        res_wrap = await process_candidate_message(
            db,
            session_row,
            client_message_id="msg-qna-2",
            content="Mình nắm rõ rồi, mình không còn câu hỏi nào nữa. Cảm ơn bạn!",
            duration_seconds=420,
        )
        assert res_wrap["sessionStatus"] == "CLOSED"
        assert res_wrap["completed"] is True
        assert res_wrap["endReason"] == "COMPLETED"
        assert db.session_row["status"] == "CLOSED"
        assert db.session_row["end_reason"] == "COMPLETED"
