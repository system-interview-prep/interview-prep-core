"""Persist the clarification questions asked and the answers given.

Both used to live only in React state. A reload dropped every answer the
candidate had typed and sent the page back through Jev and the question model
for a fresh set of questions, so the same requirement could be asked in
different words each time and nothing a candidate wrote survived the trip.

The store is keyed on the matching run - the ``candidate_job_fit_attempts``
row written for each assessment - and the requirement within it. The question
is written when it is generated, the answer is merged onto the same row when
it arrives, and a reload of that run reads both back without a single provider
call. Starting a fresh assessment of the same CV against the same posting is a
new run, so it asks afresh and leaves the earlier run's record intact.

A caller with no run id - the admin matching view, which records no attempt -
reads and writes nothing rather than sharing one unkeyed bucket.

Persistence here is never allowed to fail a completed assessment - a lost
write costs a reload, a raised exception would cost the whole result - so each
entry point rolls back and logs instead of propagating.
"""

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.matching.clarifications.models import ClarificationAnswerOutcome
from src.modules.matching.clarifications.question_generation import ClarificationQuestion

logger = logging.getLogger(__name__)

_UPSERT_QUESTION = """
INSERT INTO clarification_sessions
  (user_id, cv_id, job_id, fit_attempt_id, requirement_id, question_text,
   missing_dimension, confidence, semantic_alignment_score, evidence_refs)
VALUES
  (:user_id, :cv_id, :job_id, CAST(:fit_attempt_id AS uuid), :requirement_id,
   :question_text, :missing_dimension, :confidence, :semantic_alignment_score,
   CAST(:evidence_refs AS jsonb))
ON CONFLICT (fit_attempt_id, requirement_id) DO UPDATE SET
  -- An unanswered question may be rewritten; an answered one is the question
  -- the candidate actually replied to and must stay exactly as it was asked.
  question_text = CASE
    WHEN clarification_sessions.answer_text IS NULL THEN EXCLUDED.question_text
    ELSE clarification_sessions.question_text END,
  missing_dimension = CASE
    WHEN clarification_sessions.answer_text IS NULL THEN EXCLUDED.missing_dimension
    ELSE clarification_sessions.missing_dimension END,
  confidence = CASE
    WHEN clarification_sessions.answer_text IS NULL THEN EXCLUDED.confidence
    ELSE clarification_sessions.confidence END,
  semantic_alignment_score = CASE
    WHEN clarification_sessions.answer_text IS NULL THEN EXCLUDED.semantic_alignment_score
    ELSE clarification_sessions.semantic_alignment_score END,
  evidence_refs = CASE
    WHEN clarification_sessions.answer_text IS NULL THEN EXCLUDED.evidence_refs
    ELSE clarification_sessions.evidence_refs END,
  updated_at = now()
"""

_SAVE_ANSWER = """
UPDATE clarification_sessions SET
  answer_text = :answer_text,
  answered_at = now(),
  outcome_status = :outcome_status,
  outcome_reason_code = :outcome_reason_code,
  outcome_explanation = :outcome_explanation,
  updated_at = now()
WHERE fit_attempt_id = CAST(:fit_attempt_id AS uuid)
  AND user_id = :user_id AND requirement_id = :requirement_id
"""

_LOAD = """
SELECT requirement_id, question_text, missing_dimension, confidence,
       semantic_alignment_score, evidence_refs, answer_text, outcome_status,
       outcome_reason_code, outcome_explanation
FROM clarification_sessions
WHERE fit_attempt_id = CAST(:fit_attempt_id AS uuid) AND user_id = :user_id
ORDER BY created_at
"""


async def _rollback_quietly(db: AsyncSession) -> None:
    """Undo a failed write without raising from the recovery path itself.

    The whole point of catching here is that persistence may not fail a
    completed assessment, so the rollback may not be the thing that does.
    """
    try:
        await db.rollback()
    except Exception:  # noqa: BLE001
        logger.debug("Clarification store rollback failed", exc_info=True)


def _scope(user: dict, cv_id: str, job_id: str, fit_attempt_id: str) -> dict[str, Any]:
    return {
        "user_id": str(user["sub"]),
        "cv_id": cv_id,
        "job_id": job_id,
        "fit_attempt_id": fit_attempt_id,
    }


async def save_questions(
    db: AsyncSession,
    *,
    user: dict,
    cv_id: str,
    job_id: str,
    fit_attempt_id: str | None,
    questions: list[ClarificationQuestion],
) -> int:
    """Record the questions as asked. Returns how many rows were written."""
    if not questions or not fit_attempt_id:
        return 0
    scope = _scope(user, cv_id, job_id, fit_attempt_id)
    try:
        await db.execute(
            text(_UPSERT_QUESTION),
            [
                {
                    **scope,
                    "requirement_id": question.requirement_id,
                    "question_text": question.question_text,
                    "missing_dimension": question.missing_dimension,
                    "confidence": question.confidence,
                    "semantic_alignment_score": question.semantic_alignment_score,
                    "evidence_refs": json.dumps(question.evidence_refs),
                }
                for question in questions
            ],
        )
        await db.commit()
        return len(questions)
    except Exception:  # noqa: BLE001 - a lost write costs a reload, not a result
        await _rollback_quietly(db)
        logger.exception("Could not persist clarification questions")
        return 0


async def save_answers(
    db: AsyncSession,
    *,
    user: dict,
    cv_id: str,
    job_id: str,
    fit_attempt_id: str | None,
    answers: dict[str, str],
    outcomes: list[ClarificationAnswerOutcome],
) -> int:
    """Merge each answer and its outcome onto the question row it belongs to."""
    if not answers or not fit_attempt_id:
        return 0
    scope = _scope(user, cv_id, job_id, fit_attempt_id)
    outcome_by_id = {item.requirement_id: item for item in outcomes}
    try:
        await db.execute(
            text(_SAVE_ANSWER),
            [
                {
                    **scope,
                    "requirement_id": requirement_id,
                    "answer_text": answer_text,
                    "outcome_status": getattr(outcome_by_id.get(requirement_id), "status", None),
                    "outcome_reason_code": getattr(
                        outcome_by_id.get(requirement_id), "reason_code", None
                    ),
                    "outcome_explanation": getattr(
                        outcome_by_id.get(requirement_id), "explanation", None
                    ),
                }
                for requirement_id, answer_text in answers.items()
            ],
        )
        await db.commit()
        return len(answers)
    except Exception:  # noqa: BLE001 - see save_questions
        await _rollback_quietly(db)
        logger.exception("Could not persist clarification answers")
        return 0


async def load_session(
    db: AsyncSession, *, user: dict, cv_id: str, job_id: str, fit_attempt_id: str | None
) -> tuple[list[ClarificationQuestion], dict[str, str], list[ClarificationAnswerOutcome]]:
    """Everything a reload of one run needs: its questions, and what was answered.

    Without a run id there is nothing to restore - a new assessment asks
    afresh - so the caller gets the same empty result as an unseen run.
    """
    if not fit_attempt_id:
        return [], {}, []
    try:
        scope = _scope(user, cv_id, job_id, fit_attempt_id)
        rows = (await db.execute(text(_LOAD), scope)).mappings().all()
    except Exception:  # noqa: BLE001 - an unreadable store falls back to asking afresh
        logger.exception("Could not read stored clarifications")
        return [], {}, []

    questions: list[ClarificationQuestion] = []
    answers: dict[str, str] = {}
    outcomes: list[ClarificationAnswerOutcome] = []
    for row in rows:
        refs = row["evidence_refs"]
        if isinstance(refs, str):
            refs = json.loads(refs)
        try:
            questions.append(
                ClarificationQuestion(
                    requirementId=row["requirement_id"],
                    missingDimension=row["missing_dimension"],
                    confidence=row["confidence"],
                    evidenceRefs=list(refs or []),
                    questionText=row["question_text"],
                    semanticAlignmentScore=row["semantic_alignment_score"] or 0.0,
                )
            )
        except Exception:  # noqa: BLE001 - one unreadable row must not hide the rest
            logger.warning(
                "Skipping unreadable stored clarification for requirement %s",
                row["requirement_id"],
            )
            continue
        if row["answer_text"] is not None:
            answers[row["requirement_id"]] = row["answer_text"]
        if row["outcome_status"] is not None:
            outcomes.append(
                ClarificationAnswerOutcome(
                    requirementId=row["requirement_id"],
                    # The span id is derived from the answer text, so it is
                    # rebuilt on demand rather than stored twice.
                    evidenceRef=_answer_evidence_ref(
                        row["requirement_id"], row["answer_text"] or ""
                    ),
                    status=row["outcome_status"],
                    reasonCode=row["outcome_reason_code"],
                    explanation=row["outcome_explanation"],
                )
            )
    return questions, answers, outcomes


def _answer_evidence_ref(requirement_id: str, answer_text: str) -> str:
    from src.modules.matching.clarifications.answer_evidence import (
        build_candidate_answer_evidence,
    )

    return build_candidate_answer_evidence(requirement_id, answer_text).evidence_id
