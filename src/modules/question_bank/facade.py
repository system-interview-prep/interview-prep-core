"""Public Question Bank contract for other modules.

The interview selector uses it to file machine-generated questions for a skill
the bank does not cover yet. They enter the normal review queue (IN_REVIEW)
under a dedicated author id, so reviewers can approve them into the bank.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.question_bank.schemas import (
    AttachRubricRequest,
    CreateQuestionDraftRequest,
    RubricCriterionInput,
)
from src.modules.question_bank.service import QuestionBankService

GENERATED_QUESTION_AUTHOR = "system-question-generator"


class GeneratedQuestionRejected(ValueError):
    """The generated payload or its taxonomy mapping failed Question Bank validation."""


async def file_generated_question(
    db: AsyncSession,
    *,
    stable_key: str,
    taxonomy_version: str,
    skill_concept_id: str,
    competency_concept_id: str,
    locale: str,
    difficulty: str,
    text: str,
    objective: str,
    criteria: list[dict[str, Any]],
    timing: tuple[int, int, int],
) -> UUID:
    """Create a generated draft with its rubric and submit it for review.

    Flushes only; the caller owns the transaction. Raises
    GeneratedQuestionRejected when validation fails.
    """
    thinking, soft, hard = timing
    service = QuestionBankService(db)
    try:
        draft = CreateQuestionDraftRequest.model_validate(
            {
                "stableKey": stable_key,
                "taxonomyVersion": taxonomy_version,
                "questionType": "technical",
                "difficultyBand": difficulty,
                "canonicalLocale": locale,
                "canonicalText": text,
                "objective": objective,
                "thinkingSeconds": thinking,
                "softAnswerSeconds": soft,
                "hardAnswerSeconds": hard,
                "changeSummary": "Generated because the bank had no question for this skill.",
                "taxonomyMappings": [
                    {"conceptId": skill_concept_id, "purpose": "TARGET_SKILL", "relevance": 1},
                    {"conceptId": competency_concept_id, "purpose": "PRIMARY_COMPETENCY", "relevance": 0.6},
                ],
            }
        )
        rubric = AttachRubricRequest(
            criteria=[RubricCriterionInput.model_validate(item) for item in criteria]
        )
        version = await service.create_draft(draft, GENERATED_QUESTION_AUTHOR)
        await service.attach_rubric(version.id, GENERATED_QUESTION_AUTHOR, rubric)
        await service.submit(version.id, GENERATED_QUESTION_AUTHOR)
    except (HTTPException, ValueError) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        raise GeneratedQuestionRejected(str(detail)) from exc
    return version.id


__all__ = ["GENERATED_QUESTION_AUTHOR", "GeneratedQuestionRejected", "file_generated_question"]
