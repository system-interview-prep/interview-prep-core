"""Generate Question Bank drafts for skills the bank does not cover yet.

Last step of the selector's fallback ladder (exact skill -> broader skill ->
role question -> generated). The prompt carries only the skill label, its
specialisation, the difficulty and the locale -- never JD text. Generated
drafts are reused by later sessions before anyone reviews them, so text from
one user's uploaded JD (or an injection in it) must not reach other candidates.

Drafts are committed in their own short transaction, outside the plan-row lock
the selector holds, and filed IN_REVIEW so reviewers can approve them into the
bank. The selector then picks them up through its generated-draft step.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.trace_logging import trace_event
from src.modules.ai.facade import generate_text
from src.modules.question_bank.facade import GeneratedQuestionRejected, file_generated_question
from src.modules.question_bank.schemas import RubricCriterionInput
from src.modules.taxonomy.facade import load_active_skill_taxonomy

logger = logging.getLogger(__name__)

# Same TEXT timing as the seeded bank: thinking + soft meets the 180s floor.
GENERATED_TIMING = (30, 150, 180)
MAX_GENERATED_PER_TARGET = 2

_INSTRUCTIONS = """You write technical interview questions for a structured interview platform.
Return ONLY JSON: {"questions": [{"text": str, "objective": str, "criteria": [
  {"stableKey": str, "name": str, "description": str, "weight": number,
   "anchors": [{"level": 0, "description": str}, {"level": 1, ...}, {"level": 2, ...}, {"level": 3, ...}]}
]}]}
Rules:
- Each question tests practical understanding of the given skill at the given difficulty,
  answerable in about 3 minutes of speech, no code writing.
- 2 or 3 criteria per question; weights are positive and sum to 1.
- Anchors describe answers scoring 0 (missing/wrong) to 3 (complete, with trade-offs).
- Write text, objective, names, descriptions and anchors in the requested language.
- Questions in one response must not overlap."""


async def generate_question_payloads(
    *, skill_label: str, competency_label: str, difficulty: str, locale: str, count: int
) -> list[dict[str, Any]]:
    """Ask the LLM for ``count`` question payloads. Tests replace this function."""
    language = "Vietnamese" if locale.lower().startswith("vi") else "English"
    raw = await generate_text(
        instructions=_INSTRUCTIONS,
        input_text=json.dumps(
            {
                "skill": skill_label,
                "specialisation": competency_label,
                "difficulty": difficulty,
                "language": language,
                "count": count,
            },
            ensure_ascii=False,
        ),
        max_output_tokens=2500,
        temperature=0.4,
    )
    match = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(match.group(0) if match else raw)
    questions = data.get("questions") if isinstance(data, dict) else None
    return [item for item in questions or [] if isinstance(item, dict)]


def normalize_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Validate one LLM question against the Question Bank rubric contract."""
    question = str(payload.get("text") or "").strip()
    objective = str(payload.get("objective") or "").strip()
    raw_criteria = payload.get("criteria")
    if not question or not objective or not isinstance(raw_criteria, list) or not raw_criteria:
        return None
    try:
        weights = [Decimal(str(item.get("weight"))) for item in raw_criteria]
        total = sum(weights)
        if total <= 0 or any(weight <= 0 for weight in weights):
            return None
        # Re-normalise so rounding in the model output cannot break "sum to 1".
        scaled = [(weight / total).quantize(Decimal("0.0001")) for weight in weights]
        scaled[-1] += Decimal(1) - sum(scaled)
        criteria = []
        for order, (item, weight) in enumerate(zip(raw_criteria, scaled, strict=True)):
            key = re.sub(r"[^a-z0-9-]+", "-", str(item.get("stableKey") or f"criterion-{order + 1}").lower())
            criteria.append(
                RubricCriterionInput.model_validate(
                    {
                        "stableKey": key.strip("-")[:120] or f"criterion-{order + 1}",
                        "name": item.get("name"),
                        "description": item.get("description"),
                        "weight": weight,
                        "anchors": item.get("anchors"),
                    }
                ).model_dump(by_alias=True, mode="json")
            )
    except (ArithmeticError, TypeError, ValueError, ValidationError, AttributeError):
        return None
    if len({item["stableKey"] for item in criteria}) != len(criteria):
        return None
    return {"text": question, "objective": objective, "criteria": criteria}


async def _competency_for(
    db: AsyncSession, version: str, concept_id: str, job_role: str | None
) -> str | None:
    """The specialisation a generated question is filed under (its PRIMARY_COMPETENCY)."""
    rows = await db.execute(
        text(
            "SELECT r.source_concept_id FROM taxonomy_relations r "
            "JOIN taxonomy_concepts c ON c.taxonomy_version = r.taxonomy_version "
            "AND c.concept_id = r.source_concept_id AND c.kind = 'competency' AND c.is_active "
            "WHERE r.taxonomy_version = :version AND r.relation_type = 'REQUIRES_SKILL' "
            "AND r.target_concept_id = :concept_id ORDER BY r.source_concept_id"
        ),
        {"version": version, "concept_id": concept_id},
    )
    roles = [str(row[0]) for row in rows.all()]
    if job_role in roles:
        return job_role
    if roles:
        return roles[0]
    if job_role:
        kind = await db.scalar(
            text(
                "SELECT kind FROM taxonomy_concepts WHERE taxonomy_version = :version "
                "AND concept_id = :concept_id AND is_active"
            ),
            {"version": version, "concept_id": job_role},
        )
        if kind == "competency":
            return job_role
    return None


async def generate_and_file(
    db: AsyncSession,
    *,
    concept_id: str,
    skill_label: str,
    job_role: str | None,
    difficulty: str,
    locale: str,
    count: int,
) -> int:
    """Generate up to ``count`` drafts for one skill and commit them. Returns how many were filed."""
    if not get_settings().question_generation_enabled or count <= 0:
        return 0
    count = min(count, MAX_GENERATED_PER_TARGET)
    difficulty = difficulty if difficulty in {"foundational", "intermediate", "advanced"} else "intermediate"
    taxonomy = await load_active_skill_taxonomy(db)
    if concept_id not in taxonomy.skills:
        return 0
    competency = await _competency_for(db, taxonomy.version, concept_id, job_role)
    if competency is None:
        return 0
    competency_label = await db.scalar(
        text("SELECT label FROM taxonomy_concepts WHERE taxonomy_version = :v AND concept_id = :c"),
        {"v": taxonomy.version, "c": competency},
    )
    try:
        payloads = await generate_question_payloads(
            skill_label=skill_label,
            competency_label=str(competency_label or competency),
            difficulty=difficulty,
            locale=locale,
            count=count,
        )
    except Exception as exc:  # provider down, bad JSON: the target is dropped instead
        logger.warning("Question generation failed for %s: %s", concept_id, exc)
        return 0

    filed = 0
    slug = concept_id.removeprefix("skill-")
    for payload in payloads[:count]:
        normalized = normalize_payload(payload)
        if normalized is None:
            continue
        try:
            await file_generated_question(
                db,
                stable_key=f"gen-{slug}-{difficulty}-{locale.lower()}-{uuid4().hex[:8]}",
                taxonomy_version=taxonomy.version,
                skill_concept_id=concept_id,
                competency_concept_id=competency,
                locale=locale,
                difficulty=difficulty,
                text=normalized["text"],
                objective=normalized["objective"],
                criteria=normalized["criteria"],
                timing=GENERATED_TIMING,
            )
        except GeneratedQuestionRejected as exc:
            logger.warning("Generated question for %s rejected: %s", concept_id, exc)
            continue
        filed += 1
    if filed:
        await db.commit()
    trace_event("interviewer", "question_generated", concept_id=concept_id, requested=count, filed=filed)
    return filed
