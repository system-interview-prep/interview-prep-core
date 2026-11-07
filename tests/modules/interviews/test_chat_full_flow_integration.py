"""Opt-in PostgreSQL/API integration for the complete structured Chat path.

Run with RUN_DB_INTEGRATION_TESTS=1 after applying migrations to the configured
development PostgreSQL database. By default the interviewer and grader are
deterministic fakes. Set RUN_REAL_CHAT_EVAL=1 as well to replay the JSON scenario
through the real interviewer and grader models.
"""

from __future__ import annotations

import json
import os
from importlib import import_module
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from src.core.security import current_user
from src.infrastructure.database import SessionFactory, engine, get_db
from src.main import create_app
from src.modules.interviews.agent import interview_agent as agent_module
from src.modules.interviews.application.runtime import agent_runtime, chat_runtime
from src.modules.interviews.evaluation.evaluation_types import (
    CompetencyScore,
    DecisionRecommendation,
    SessionEvaluationResult,
    TurnEvaluationResult,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_INTEGRATION_TESTS") != "1",
    reason="PostgreSQL integration database is not enabled",
)
api_router = import_module("src.modules.interviews.api.router")
SCENARIO_PATH = Path(__file__).parents[2] / "evals" / "interviews" / "chat_backend_star_v1.json"


class _DeterministicChatEngine:
    """Advance warm-up -> technical question -> closing without an LLM call."""

    def __init__(self, **_kwargs):
        pass

    async def handle_turn(self, turn_input, session_state):
        from src.modules.interviews.core.contracts.interview import (
            InterviewerTurnOutput,
            InterviewStage,
            TurnAction,
        )

        stages = [
            InterviewStage.DEEP_DIVE,
            InterviewStage.BEHAVIORAL,
            InterviewStage.CLOSING,
        ]
        is_warmup = turn_input.turn_index == 0
        next_stage = stages[min(turn_input.turn_index, len(stages) - 1)]
        return InterviewerTurnOutput(
            session_id=turn_input.session_id,
            turn_index=turn_input.turn_index,
            message_text="Cảm ơn bạn. Hãy kể thêm về kinh nghiệm của bạn."
            if is_warmup
            else "Cảm ơn bạn đã chia sẻ. Mình xin kết thúc buổi phỏng vấn tại đây.",
            action=TurnAction.NEXT_QUESTION,
            current_stage=next_stage,
            current_competency="Giới thiệu bản thân" if is_warmup else "Phỏng vấn Chat",
            metadata={},
        )


class _DeterministicGrader:
    async def evaluate_session(self, *, session_id, turns_input, is_vi):
        scored_turns = [turn for turn in turns_input if turn.weight > 0]
        detail = [
            TurnEvaluationResult(
                turn_id=turn.turn_id,
                score=7.5,
                feedback="Câu trả lời có thông tin liên quan và dễ theo dõi.",
                evidence_quotes=[turn.candidate_answer[:120]],
                strengths=["Trình bày rõ ràng"],
            )
            for turn in scored_turns
        ]
        return SessionEvaluationResult(
            session_id=session_id,
            overall_score=7.5,
            decision_recommendation=DecisionRecommendation.CONSIDER,
            competency_scores=[
                CompetencyScore(competency=turn.competency, score=7.5, weight=turn.weight)
                for turn in scored_turns
            ],
            turn_evaluations=detail,
            recruiter_summary="Ứng viên hoàn thành phần giới thiệu và câu hỏi chuyên môn.",
            candidate_feedback="Tiếp tục bổ sung ví dụ cụ thể cho câu trả lời chuyên môn.",
            next_round_topics=["Thiết kế API"],
        )


@pytest.mark.asyncio
async def test_chat_interview_runs_from_opening_through_persisted_report(monkeypatch) -> None:
    scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    use_real_models = os.getenv("RUN_REAL_CHAT_EVAL") == "1"
    if use_real_models:
        from src.core.config import get_settings

        if not get_settings().openai_api_key:
            pytest.skip("RUN_REAL_CHAT_EVAL=1 requires OPENAI_API_KEY")

    user_id = str(uuid4())
    session_id = str(uuid4())
    plan_id = str(uuid4())
    turn_ids = [str(uuid4()) for _ in scenario["turns"]]
    user = {"sub": user_id, "email": f"{user_id}@example.test", "roles": ["CANDIDATE"]}

    async def override_db():
        async with SessionFactory() as db:
            yield db

    async def no_rate_limit(*_args, **_kwargs):
        return None

    if not use_real_models:
        monkeypatch.setattr(chat_runtime, "InterviewCoreEngine", _DeterministicChatEngine)
        monkeypatch.setattr(agent_module, "InterviewCoreEngine", _DeterministicChatEngine)
        monkeypatch.setattr(agent_runtime, "evaluate_closed_session", _evaluate_with_fake_grader)
    monkeypatch.setattr(api_router.interview_rate_limiter, "check", no_rate_limit)

    app = create_app()
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[current_user] = lambda: user

    async with SessionFactory() as db:
        try:
            await db.execute(
                text(
                    "INSERT INTO users (id, email, name, provider) "
                    "VALUES (:id, :email, 'Chat E2E Candidate', 'local')"
                ),
                {"id": user_id, "email": user["email"]},
            )
            await db.execute(
                text(
                    "INSERT INTO interview_sessions "
                    "(id, user_id, type, language, status, mode, locale, duration_minutes, experience_type) "
                    "VALUES (:id, :uid, 'Chat', 'Vietnamese', 'OPEN', 'text', 'vi-VN', 25, 'interview_chat')"
                ),
                {"id": session_id, "uid": user_id},
            )
            await db.execute(
                text(
                    "INSERT INTO interview_session_plans "
                    "(id, session_id, status, source_context, plan_payload) "
                    "VALUES (:id, :sid, 'LOCKED', '{}'::jsonb, '{}'::jsonb)"
                ),
                {"id": plan_id, "sid": session_id},
            )
            await db.execute(
                text(
                    "INSERT INTO interview_turns (id, session_id, turn_index, status, question_snapshot) "
                    "VALUES (:id, :sid, :turn_index, 'PLANNED', CAST(:snapshot AS jsonb))"
                ),
                [
                    {
                        "id": turn_id,
                        "sid": session_id,
                        "turn_index": index,
                        "snapshot": json.dumps(
                            {
                                "stage": turn["stage"],
                                "questionText": turn["question"],
                                "taxonomyTarget": {"label": turn["competency"]},
                                "target": turn.get("target", {}),
                            },
                            ensure_ascii=False,
                        ),
                    }
                    for index, (turn_id, turn) in enumerate(zip(turn_ids, scenario["turns"], strict=True))
                ],
            )
            await db.commit()

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                opened = await client.post(f"/api/v1/interviews/sessions/{session_id}/chat/start")
                assert opened.status_code == 200, opened.text
                assert opened.json()["currentTurn"]["questionText"] == scenario["turns"][0]["question"]

                submitted_answer_count = 0
                for turn_index, turn in enumerate(scenario["turns"]):
                    for response_index, answer in enumerate(turn["messages"]):
                        reply = await client.post(
                            f"/api/v1/interviews/sessions/{session_id}/chat/message",
                            json={
                                "clientMessageId": f"{scenario['id']}-{turn_index}-{response_index}",
                                "content": answer,
                            },
                        )
                        assert reply.status_code == 200, reply.text
                        submitted_answer_count += 1
                        reply_data = reply.json()
                        if reply_data.get("currentTurnIndex", -1) > turn_index:
                            break
                    assert reply_data.get("currentTurnIndex", -1) > turn_index or (
                        turn_index == len(scenario["turns"]) - 1
                        and reply_data.get("currentStage") == "CLOSING"
                    ), f"Chat did not advance after {turn['stage']}: {reply_data}"

                closed = await client.post(
                    f"/api/v1/interviews/sessions/{session_id}/chat/complete",
                    json={"reason": "USER_ENDED"},
                )
                assert closed.status_code == 200, closed.text
                assert closed.json()["sessionStatus"] == "CLOSED"
                closed_end_reason = closed.json()["endReason"]

                evaluation = await client.post(f"/api/v1/interviews/sessions/{session_id}/evaluate")
                assert evaluation.status_code == 200, evaluation.text
                evaluation_data = evaluation.json()
                if use_real_models:
                    assert 0 <= float(evaluation_data["overall_score"]) <= 10
                else:
                    assert evaluation_data["overall_score"] == 7.5
                assert "decision_recommendation" not in evaluation.json()

                report = await client.get(f"/api/v1/interviews/sessions/{session_id}/evaluation")
                assert report.status_code == 200, report.text
                report_data = report.json()
                if use_real_models:
                    assert 0 <= float(report_data["overall_score"]) <= 10
                    assert len(report_data["turn_evaluations"]) >= scenario["evalAssertions"][
                        "minimumScoredTurns"
                    ]
                    candidate_messages = [
                        message for turn in scenario["turns"] for message in turn["messages"]
                    ]
                    evidence_quotes = [
                        quote
                        for turn_result in report_data["turn_evaluations"]
                        for quote in turn_result.get("evidence_quotes", [])
                    ]
                    assert any(quote in answer for quote in evidence_quotes for answer in candidate_messages)
                else:
                    assert report_data["overall_score"] == 7.5
                    assert len(report_data["turn_evaluations"]) == 2

            async with SessionFactory() as verify_db:
                persisted = await verify_db.execute(
                    text(
                        "SELECT s.status, s.end_reason, "
                        "(SELECT COUNT(*) FROM interview_chat_messages m "
                        "WHERE m.session_id=s.id AND m.role='user') AS user_messages, "
                        "(SELECT COUNT(*) FROM interview_evaluations e WHERE e.session_id=s.id) AS reports "
                        "FROM interview_sessions s WHERE s.id=:sid"
                    ),
                    {"sid": session_id},
                )
                row = persisted.mappings().one()
                assert row["status"] == "CLOSED"
                assert row["end_reason"] == closed_end_reason
                assert row["user_messages"] == submitted_answer_count
                assert row["reports"] == 1
        finally:
            await db.rollback()
            await db.execute(text("DELETE FROM users WHERE id=:id"), {"id": user_id})
            await db.commit()
            app.dependency_overrides.clear()
            await engine.dispose()


async def _evaluate_with_fake_grader(db, session_id: str, _engine=None):
    from src.modules.interviews.evaluation.evaluation_service import evaluate_closed_session

    return await evaluate_closed_session(db, session_id, engine=_DeterministicGrader())
