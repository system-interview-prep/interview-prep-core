# src/modules/interviews/evaluation/evaluation_service.py
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.trace_logging import trace_event
from src.modules.interviews.evaluation.evaluation_engine import InterviewEvaluationEngine
from src.modules.interviews.evaluation.evaluation_types import (
    DecisionRecommendation,
    SessionEvaluationResult,
    TurnEvaluationInput,
)

logger = logging.getLogger("InterviewEvaluationService")

# Share of the planned agenda that must have been assessed before a report may
# recommend hiring. Below this, the decision is capped at CONSIDER.
_MIN_COVERAGE_FOR_CONFIDENT_DECISION = 0.6


class EvaluationServiceError(RuntimeError):
    pass


# Preset turns (no Question Bank rubric) count half of an average JD competency.
_PRESET_TURN_WEIGHT = {"WARM_UP": 0.0, "VALIDATE": 0.5, "BEHAVIORAL": 0.5}


def _turn_weight(stage: str, snap: dict, importance_by_concept: Dict[str, float]) -> float:
    """Weight of one turn's competency in the overall score.

    Technical competencies follow the planner's importance (must-have and
    heavier requirements count more), scaled so the average technical
    competency weighs 1. Previously every competency weighed 1, so a single
    VALIDATE or BEHAVIORAL preset counted as much as a must-have competency
    with three questions.
    """
    if stage in _PRESET_TURN_WEIGHT:
        return _PRESET_TURN_WEIGHT[stage]
    concept_id = (snap.get("taxonomyTarget") or {}).get("conceptId") or (snap.get("target") or {}).get("conceptId")
    importance = importance_by_concept.get(str(concept_id)) if concept_id else None
    if not importance or not importance_by_concept:
        return 1.0
    return round(importance * len(importance_by_concept), 6)


async def evaluate_closed_session(
    db: AsyncSession,
    session_id: str,
    engine: Optional[InterviewEvaluationEngine] = None,
) -> Optional[SessionEvaluationResult]:
    """
    Trích xuất dữ liệu từ PostgreSQL, gọi P4 Evaluation Engine và lưu kết quả vào CSDL.

    Trả về None khi một request đồng thời đã tạo xong báo cáo trong lúc request
    này chờ lock.
    """
    started_at = time.monotonic()
    trace_event("interviewer", "evaluation_pipeline_started", session_id=session_id)
    eval_engine = engine or InterviewEvaluationEngine()

    # 1. Kiểm tra session
    sess_res = await db.execute(
        # Serialize evaluations of one session: the results page auto-triggers
        # /evaluate on 404, so a refresh or second tab raced two full LLM
        # gradings into DELETE+INSERT on the unique session_id (500).
        text(
            "SELECT id, status, locale, duration_minutes FROM interview_sessions "
            "WHERE id = :sid FOR UPDATE"
        ),
        {"sid": session_id},
    )
    session_row = sess_res.mappings().one_or_none()
    if not session_row:
        raise EvaluationServiceError(f"Phiên phỏng vấn {session_id} không tồn tại.")
    if session_row["status"] != "CLOSED":
        raise EvaluationServiceError(
            f"Phiên phỏng vấn {session_id} phải kết thúc trước khi đánh giá."
        )

    already_evaluated = await db.scalar(
        text("SELECT 1 FROM interview_evaluations WHERE session_id = :sid"),
        {"sid": session_id},
    )
    if already_evaluated:
        return None

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

    # Planner importance per competency (normalized to sum 1 by P1).
    importance_res = await db.execute(
        text(
            """
            SELECT t.concept_id, t.importance
            FROM session_competency_targets t
            JOIN interview_session_plans p ON p.id = t.plan_id
            WHERE p.session_id = :sid
            """
        ),
        {"sid": session_id},
    )
    importance_by_concept = {
        str(row["concept_id"]): float(row["importance"] or 0)
        for row in importance_res.mappings().all()
    }

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
    #
    # A turn still in PLANNED (or SKIPPED by the behavioral reserve jump) was
    # never put to the candidate: the pacing controller ran out of time before
    # reaching it. Scoring it would hand the
    # candidate a zero-weighted "did not answer" for a question the system chose
    # not to ask. Those competencies are reported as uncovered instead.
    turns_input: List[TurnEvaluationInput] = []
    uncovered_competencies: List[str] = []
    for t in turns:
        t_id = str(t["id"])
        snap = t.get("question_snapshot") or {}
        if str(t.get("status") or "").upper() in {"PLANNED", "SKIPPED"}:
            label = (
                (snap.get("taxonomyTarget") or {}).get("label")
                or (snap.get("target") or {}).get("conceptId")
            )
            if label and label not in uncovered_competencies:
                uncovered_competencies.append(str(label))
            continue
        stage = snap.get("stage") or ("WARM_UP" if t["turn_index"] == 0 else "DEEP_DIVE")
        # The closing Q&A is the candidate asking the interviewer questions; it
        # is not an assessed answer and must not count as a competency.
        if stage == "CLOSING":
            continue
        # Ưu tiên answer_text đã lưu, nếu không gom từ tin nhắn chat
        ans = t.get("answer_text")
        if not ans and t_id in msgs_by_turn:
            ans = "\n\n".join(msgs_by_turn[t_id])
        # A question that was on screen when the session ended (timeout or user
        # ending) never got an answer. Grading it as "did not answer" penalises
        # the candidate for the clock; report it as uncovered instead.
        if not ans and str(t.get("status") or "").upper() == "ASKED":
            label = (
                (snap.get("taxonomyTarget") or {}).get("label")
                or (snap.get("target") or {}).get("conceptId")
            )
            if label and label not in uncovered_competencies:
                uncovered_competencies.append(str(label))
            continue
        q_prompt = snap.get("questionText") or snap.get("question_text") or "Câu hỏi chuyên môn"
        weight = _turn_weight(stage, snap, importance_by_concept)
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
                stage=str(stage or ""),
            )
        )

    # WARM_UP carries weight 0, so a session abandoned right after the greeting
    # has turns but nothing scorable. Grading it anyway produced a 0.0/REJECT
    # report for a candidate who was never actually assessed.
    if not any(t.weight > 0 for t in turns_input):
        raise EvaluationServiceError(
            f"Phiên phỏng vấn {session_id} chưa có lượt trả lời chuyên môn nào để đánh giá."
        )

    # 5. Gọi Evaluation Engine
    result: SessionEvaluationResult = await eval_engine.evaluate_session(
        session_id=session_id,
        turns_input=turns_input,
        is_vi=is_vi,
    )

    # Competencies the selector dropped because nothing in the bank (nor its
    # fallback ladder) could cover them; they count against coverage too.
    dropped = await db.scalar(
        text(
            "SELECT plan_payload -> 'questionSelection' -> 'uncoveredTargets' "
            "FROM interview_session_plans WHERE session_id = :sid"
        ),
        {"sid": session_id},
    )
    dropped_labels: set[str] = set()
    for item in dropped if isinstance(dropped, list) else []:
        label = item.get("label") or item.get("conceptId") if isinstance(item, dict) else None
        if label and label not in uncovered_competencies:
            uncovered_competencies.append(str(label))
            dropped_labels.add(str(label))

    # Generated questions are used before review; say so on the report.
    unreviewed = sum(
        (t.get("question_snapshot") or {}).get("questionSource") == "generated_unreviewed"
        and str(t.get("status") or "").upper() not in {"PLANNED", "SKIPPED"}
        for t in turns
    )
    if unreviewed:
        result.red_flags = [
            (
                f"{unreviewed} câu hỏi do AI sinh tự động, chưa qua kiểm duyệt của Question Bank."
                if is_vi
                else f"{unreviewed} question(s) were AI-generated and not yet reviewed in the Question Bank."
            ),
            *(result.red_flags or []),
        ]

    # Surface what the session never got to, so the report cannot be read as
    # full coverage of the planned agenda. Skipping those turns stops the
    # candidate being penalised for unasked questions, but it would otherwise
    # also let a session that ended after one good answer produce a confident
    # PASS off a single data point.
    scored_count = len(turns_input)
    uncovered_count = len(uncovered_competencies)
    if uncovered_count:
        coverage_ratio = scored_count / (scored_count + uncovered_count)
        trace_event(
            "interviewer",
            "evaluation_partial_coverage",
            session_id=session_id,
            scored_turns=scored_count,
            uncovered_count=uncovered_count,
            coverage_ratio=round(coverage_ratio, 4),
            uncovered=uncovered_competencies,
        )

        existing_topics = list(result.next_round_topics or [])
        for label in uncovered_competencies:
            if label in dropped_labels:
                topic = (
                    f"{label} (chưa có câu hỏi phù hợp trong phiên này)"
                    if is_vi
                    else f"{label} (no suitable question in this session)"
                )
            else:
                topic = (
                    f"{label} (chưa kịp hỏi trong phiên này)"
                    if is_vi
                    else f"{label} (not covered in this session)"
                )
            if topic not in existing_topics:
                existing_topics.append(topic)
        result.next_round_topics = existing_topics

        total_planned = scored_count + uncovered_count
        coverage_flag = (
            f"Phiên chưa phủ hết nội dung: chỉ đánh giá được {scored_count}/{total_planned} lượt."
            if is_vi
            else f"Partial coverage: only {scored_count}/{total_planned} planned turns were assessed."
        )
        result.red_flags = [coverage_flag, *(result.red_flags or [])]

        # A hiring recommendation needs enough evidence behind it. Below the
        # threshold the score still stands, but the decision is capped so a
        # thin session cannot read as a confident PASS.
        if coverage_ratio < _MIN_COVERAGE_FOR_CONFIDENT_DECISION and result.decision_recommendation in {
            DecisionRecommendation.STRONG_PASS,
            DecisionRecommendation.PASS,
        }:
            trace_event(
                "interviewer",
                "evaluation_decision_capped",
                session_id=session_id,
                original_decision=result.decision_recommendation.value,
                coverage_ratio=round(coverage_ratio, 4),
            )
            result.decision_recommendation = DecisionRecommendation.CONSIDER

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
    trace_event(
        "interviewer",
        "evaluation_pipeline_completed",
        session_id=session_id,
        overall_score=result.overall_score,
        decision=result.decision_recommendation.value,
        turns_evaluated=len(result.turn_evaluations),
        duration_ms=round((time.monotonic() - started_at) * 1000),
    )
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
