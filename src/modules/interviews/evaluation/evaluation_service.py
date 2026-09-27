# src/modules/interviews/evaluation/evaluation_service.py
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.interviews.evaluation.evaluation_engine import InterviewEvaluationEngine
from src.modules.interviews.evaluation.evaluation_types import (
    CompetencyScore,
    DecisionRecommendation,
    SessionEvaluationResult,
    StarAnalysis,
    TurnEvaluationInput,
    TurnEvaluationResult,
)

logger = logging.getLogger("InterviewEvaluationService")


class EvaluationServiceError(RuntimeError):
    pass


async def evaluate_closed_session(
    db: AsyncSession,
    session_id: str,
    engine: Optional[InterviewEvaluationEngine] = None,
) -> SessionEvaluationResult:
    """
    Trích xuất dữ liệu từ PostgreSQL, gọi P4 Evaluation Engine và lưu kết quả vào CSDL.
    """
    eval_engine = engine or InterviewEvaluationEngine()

    # 1. Kiểm tra session
    sess_res = await db.execute(
        text("SELECT id, status, locale, duration_minutes FROM interview_sessions WHERE id = :sid"),
        {"sid": session_id},
    )
    session_row = sess_res.mappings().one_or_none()
    if not session_row:
        raise EvaluationServiceError(f"Phiên phỏng vấn {session_id} không tồn tại.")

    is_vi = str(session_row.get("locale") or "vi").lower().startswith("vi")

    # 2. Lấy danh sách turns
    turns_res = await db.execute(
        text(
            """
            SELECT id, turn_index, status, question_snapshot, answer_text
            FROM interview_turns
            WHERE session_id = :sid
            ORDER BY turn_index ASC
            """
        ),
        {"sid": session_id},
    )
    turns = [dict(r) for r in turns_res.mappings().all()]
    if not turns:
        raise EvaluationServiceError(f"Phiên phỏng vấn {session_id} không có câu hỏi nào để đánh giá.")

    # 3. Lấy tin nhắn người dùng (nếu answer_text trong turn chưa được tích lũy đầy đủ)
    msgs_res = await db.execute(
        text(
            """
            SELECT turn_id, content
            FROM interview_chat_messages
            WHERE session_id = :sid AND role = 'user'
            ORDER BY sequence ASC
            """
        ),
        {"sid": session_id},
    )
    user_msgs = [dict(r) for r in msgs_res.mappings().all()]
    msgs_by_turn: Dict[str, List[str]] = {}
    for m in user_msgs:
        t_id = m.get("turn_id")
        if t_id:
            if t_id not in msgs_by_turn:
                msgs_by_turn[t_id] = []
            msgs_by_turn[t_id].append(m["content"])

    # 4. Chuẩn bị danh sách TurnEvaluationInput
    turns_input: List[TurnEvaluationInput] = []
    for t in turns:
        t_id = str(t["id"])
        snap = t.get("question_snapshot") or {}
        q_prompt = snap.get("questionText") or snap.get("question_text") or "Câu hỏi chuyên môn"
        stage = snap.get("stage") or ("WARM_UP" if t["turn_index"] == 0 else "DEEP_DIVE")
        # Turn 0 (WARM_UP) là lượt chào hỏi phá băng, miễn trừ tính vào điểm trung bình kỹ thuật
        weight = 0.0 if stage == "WARM_UP" else 1.0
        competency = (
            (snap.get("taxonomyTarget") or {}).get("label")
            or (snap.get("target") or {}).get("conceptId")
            or ("Khởi động & Tác phong" if stage == "WARM_UP" else "Kỹ thuật chuyên môn")
        )
        criteria_raw = (snap.get("rubric") or {}).get("criteria") or ""
        if isinstance(criteria_raw, list):
            parts = []
            for c in criteria_raw:
                if isinstance(c, dict):
                    name = c.get("name") or c.get("stableKey") or "Tiêu chí"
                    desc = c.get("description") or ""
                    parts.append(f"- {name}: {desc}")
                else:
                    parts.append(f"- {c}")
            criteria = "\n".join(parts)
        elif isinstance(criteria_raw, dict):
            criteria = json.dumps(criteria_raw, ensure_ascii=False)
        else:
            criteria = str(criteria_raw or "")

        # Ưu tiên answer_text đã lưu, nếu không gom từ tin nhắn chat
        ans = t.get("answer_text")
        if not ans and t_id in msgs_by_turn:
            ans = "\n\n".join(msgs_by_turn[t_id])

        if not ans:
            ans = "Ứng viên không trả lời hoặc bỏ qua câu hỏi này."

        turns_input.append(
            TurnEvaluationInput(
                turn_id=t_id,
                turn_index=t["turn_index"],
                competency=competency,
                question_prompt=q_prompt,
                rubric_criteria=criteria,
                candidate_answer=ans,
                weight=weight,
            )
        )

    # 5. Gọi Evaluation Engine
    result: SessionEvaluationResult = await eval_engine.evaluate_session(
        session_id=session_id,
        turns_input=turns_input,
        is_vi=is_vi,
    )

    # 6. Lưu vào PostgreSQL
    eval_id = str(uuid4())
    now = datetime.now(timezone.utc)

    # Kiểm tra xem đã có evaluation cho session này chưa (upsert / replace)
    existing_eval = await db.scalar(
        text("SELECT id FROM interview_evaluations WHERE session_id = :sid"),
        {"sid": session_id},
    )
    if existing_eval:
        await db.execute(
            text("DELETE FROM interview_evaluations WHERE id = :eid"),
            {"eid": existing_eval},
        )

    competency_scores_json = json.dumps([cs.model_dump() for cs in result.competency_scores])

    await db.execute(
        text(
            """
            INSERT INTO interview_evaluations
            (id, session_id, overall_score, decision_recommendation, competency_scores,
             recruiter_summary, candidate_feedback, next_round_topics, red_flags, evaluated_at, created_at)
            VALUES
            (:id, :sid, :overall_score, :decision, CAST(:comp_scores AS jsonb),
             :recruiter_summary, :candidate_feedback, :next_round_topics, :red_flags, :evaluated_at, now())
            """
        ),
        {
            "id": eval_id,
            "sid": session_id,
            "overall_score": result.overall_score,
            "decision": result.decision_recommendation.value,
            "comp_scores": competency_scores_json,
            "recruiter_summary": result.recruiter_summary,
            "candidate_feedback": result.candidate_feedback,
            "next_round_topics": result.next_round_topics,
            "red_flags": result.red_flags,
            "evaluated_at": now,
        },
    )

    # Lưu chi tiết từng turn
    for te in result.turn_evaluations:
        turn_eval_id = str(uuid4())
        star_json = json.dumps(te.star_analysis.model_dump())
        await db.execute(
            text(
                """
                INSERT INTO interview_turn_evaluations
                (id, evaluation_id, turn_id, score, star_analysis, evidence_quotes,
                 feedback, strengths, weaknesses, created_at)
                VALUES
                (:id, :eval_id, :turn_id, :score, CAST(:star_json AS jsonb), :evidence_quotes,
                 :feedback, :strengths, :weaknesses, now())
                """
            ),
            {
                "id": turn_eval_id,
                "eval_id": eval_id,
                "turn_id": te.turn_id,
                "score": te.score,
                "star_json": star_json,
                "evidence_quotes": te.evidence_quotes,
                "feedback": te.feedback,
                "strengths": te.strengths,
                "weaknesses": te.weaknesses,
            },
        )

    await db.commit()
    return result


async def get_session_evaluation(db: AsyncSession, session_id: str) -> Optional[Dict[str, Any]]:
    """Truy vấn báo cáo đánh giá đã lưu của session."""
    eval_res = await db.execute(
        text(
            """
            SELECT id, session_id, overall_score, decision_recommendation,
                   competency_scores, recruiter_summary, candidate_feedback,
                   next_round_topics, red_flags, evaluated_at, created_at
            FROM interview_evaluations
            WHERE session_id = :sid
            """
        ),
        {"sid": session_id},
    )
    row = eval_res.mappings().one_or_none()
    if not row:
        return None

    eval_data = dict(row)
    eval_id = eval_data["id"]

    turns_res = await db.execute(
        text(
            """
            SELECT ite.id, ite.evaluation_id, ite.turn_id, ite.score, ite.star_analysis,
                   ite.evidence_quotes, ite.feedback, ite.strengths, ite.weaknesses, ite.created_at,
                   it.turn_index, it.question_snapshot
            FROM interview_turn_evaluations ite
            LEFT JOIN interview_turns it ON it.id = ite.turn_id
            WHERE ite.evaluation_id = :eid
            ORDER BY it.turn_index ASC NULLS LAST, ite.created_at ASC
            """
        ),
        {"eid": eval_id},
    )
    turn_rows = [dict(r) for r in turns_res.mappings().all()]

    turn_evaluations = []
    for tr in turn_rows:
        snapshot = tr.get("question_snapshot") or {}
        taxonomy = snapshot.get("taxonomyTarget") or {}
        comp_label = taxonomy.get("label") or "Chuyên môn"
        q_text = snapshot.get("questionText") or ""
        t_index = (tr.get("turn_index") + 1) if tr.get("turn_index") is not None else 1
        star_data = tr.get("star_analysis") or {}

        item = {
            "id": tr["id"],
            "turn_id": tr["turn_id"],
            "turnId": tr["turn_id"],
            "turn_index": t_index,
            "turnIndex": t_index,
            "competency": comp_label,
            "stage": snapshot.get("stage") or "DEEP_DIVE",
            "question_text": q_text,
            "questionText": q_text,
            "score": float(tr["score"]),
            "star_analysis": star_data,
            "starAnalysis": star_data,
            "evidence_quotes": tr.get("evidence_quotes") or [],
            "evidenceQuotes": tr.get("evidence_quotes") or [],
            "feedback": tr.get("feedback") or "",
            "strengths": tr.get("strengths") or [],
            "weaknesses": tr.get("weaknesses") or [],
        }
        turn_evaluations.append(item)

    comp_scores = eval_data.get("competency_scores") or []
    eval_at_str = eval_data["evaluated_at"].isoformat() if eval_data.get("evaluated_at") else None

    return {
        "id": eval_data["id"],
        "session_id": eval_data["session_id"],
        "sessionId": eval_data["session_id"],
        "overall_score": float(eval_data["overall_score"]),
        "overallScore": float(eval_data["overall_score"]),
        "decision_recommendation": eval_data["decision_recommendation"],
        "decisionRecommendation": eval_data["decision_recommendation"],
        "competency_scores": comp_scores,
        "competencyScores": comp_scores,
        "recruiter_summary": eval_data["recruiter_summary"],
        "recruiterSummary": eval_data["recruiter_summary"],
        "candidate_feedback": eval_data["candidate_feedback"],
        "candidateFeedback": eval_data["candidate_feedback"],
        "next_round_topics": eval_data.get("next_round_topics") or [],
        "nextRoundTopics": eval_data.get("next_round_topics") or [],
        "red_flags": eval_data.get("red_flags") or [],
        "redFlags": eval_data.get("red_flags") or [],
        "evaluated_at": eval_at_str,
        "evaluatedAt": eval_at_str,
        "turn_evaluations": turn_evaluations,
        "turnEvaluations": turn_evaluations,
    }
