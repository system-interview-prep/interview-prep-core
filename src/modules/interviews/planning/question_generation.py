"""Generate Question Bank drafts for skills the bank does not cover yet.

Last step of the selector's fallback ladder (exact skill -> broader skill ->
role question -> generated). The prompt carries only the skill label, its
specialisation, the difficulty and the locale -- never JD text. Generated
drafts are reused by later sessions before anyone reviews them, so text from
one user's uploaded JD (or an injection in it) must not reach other candidates.

Drafts are committed in their own short transaction, outside the plan-row lock
the selector holds, and filed IN_REVIEW so reviewers can approve them into the
bank. The selector then picks them up through its generated-draft step.

Every draft passes an automatic quality gate before it is filed: rubric
structure, language, no near-duplicate of a question the skill already has,
and a second LLM call that judges skill fit, difficulty and soundness. The gate
fails closed -- if the judge is unavailable nothing is filed -- because drafts
are asked and scored before a human reviews them.
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
# Session-time generation stays small (the candidate is waiting); background
# pre-generation asks for enough variants to rotate (question_coverage).
MAX_GENERATED_PER_TARGET = 2
MAX_PREGENERATED_PER_TARGET = 3
# One regeneration round for questions the gate rejected.
_GATE_ATTEMPTS = 2
# Token-set Jaccard similarity at or above this is a near-duplicate.
DUPLICATE_SIMILARITY = 0.6

_INSTRUCTIONS = """You write technical interview questions for a structured interview platform.
Return ONLY JSON: {"questions": [{"text": str, "objective": str, "criteria": [
  {"stableKey": str, "name": str, "description": str, "weight": number,
   "anchors": [{"level": 0, "description": str}, {"level": 1, ...}, {"level": 2, ...}, {"level": 3, ...}]}
]}]}
Rules:
- Each question tests practical understanding of the given skill at the given difficulty,
  answerable in about 3 minutes of speech, no code writing.
- Phrase it the way an interviewer says it out loud: one question, at most 25 words,
  no preamble, no multi-part lists of sub-questions.
- 2 or 3 criteria per question; weights are positive and sum to 1.
- Anchors describe answers scoring 0 (missing/wrong) to 3 (complete, with trade-offs).
- Write text, objective, names, descriptions and anchors in the requested language.
- Questions in one response must not overlap."""

_JUDGE_INSTRUCTIONS = """You review machine-written technical interview questions before candidates see them.
For each question decide:
- on_skill: it tests the given skill itself, within the given specialisation;
- difficulty_ok: it fits the given difficulty;
- verbal: it can be answered by speaking for about 3 minutes, without writing code;
- sound: the question, objective and rubric are technically correct and unambiguous;
- concise: it is one spoken question of at most about 25 words, not a list of sub-questions.
Return ONLY JSON: {"verdicts": [{"index": int, "on_skill": bool, "difficulty_ok": bool,
"verbal": bool, "sound": bool, "concise": bool, "reason": str}]} with one verdict per question, same order."""

_VI_CHARS = re.compile(
    r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", re.IGNORECASE
)
_WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


def _json_object(raw: str) -> Any:
    match = re.search(r"\{.*\}", raw, re.S)
    return json.loads(match.group(0) if match else raw)


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
    data = _json_object(raw)
    questions = data.get("questions") if isinstance(data, dict) else None
    return [item for item in questions or [] if isinstance(item, dict)]


async def judge_question_payloads(
    *, skill_label: str, competency_label: str, difficulty: str, locale: str, questions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One LLM call that judges a batch of normalized questions. Tests replace this function.

    Like generation, it sees taxonomy labels only -- never JD text.
    """
    raw = await generate_text(
        instructions=_JUDGE_INSTRUCTIONS,
        input_text=json.dumps(
            {
                "skill": skill_label,
                "specialisation": competency_label,
                "difficulty": difficulty,
                "language": "Vietnamese" if locale.lower().startswith("vi") else "English",
                "questions": [
                    {
                        "index": index,
                        "text": item["text"],
                        "objective": item["objective"],
                        "criteria": [criterion.get("name") for criterion in item["criteria"]],
                    }
                    for index, item in enumerate(questions)
                ],
            },
            ensure_ascii=False,
        ),
        max_output_tokens=1200,
        temperature=0,
    )
    data = _json_object(raw)
    verdicts = data.get("verdicts") if isinstance(data, dict) else None
    return [item for item in verdicts or [] if isinstance(item, dict)]


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


def language_matches(question: dict[str, Any], locale: str) -> bool:
    """Vietnamese drafts must read as Vietnamese; English drafts must not."""
    vi_marks = len(_VI_CHARS.findall(f"{question['text']} {question['objective']}"))
    return vi_marks >= 3 if locale.lower().startswith("vi") else vi_marks == 0


def _tokens(value: str) -> set[str]:
    return {word.casefold() for word in _WORD.findall(value)}


def is_near_duplicate(text_value: str, existing: list[str]) -> bool:
    """Token-set Jaccard check; deterministic and needs no embedding provider."""
    words = _tokens(text_value)
    if not words:
        return True
    for other in existing:
        other_words = _tokens(other)
        if other_words and len(words & other_words) / len(words | other_words) >= DUPLICATE_SIMILARITY:
            return True
    return False


def _verdict_passes(verdict: dict[str, Any] | None) -> bool:
    return bool(verdict) and all(
        verdict.get(key) is True for key in ("on_skill", "difficulty_ok", "verbal", "sound", "concise")
    )


async def gate_questions(
    *,
    payloads: list[dict[str, Any]],
    existing_texts: list[str],
    skill_label: str,
    competency_label: str,
    difficulty: str,
    locale: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Run the quality gate; returns the accepted questions and per-reason rejection counts.

    Raises when the judge call fails, so the caller files nothing (fail closed).
    """
    stats = {"structure": 0, "language": 0, "duplicate": 0, "judge": 0}
    candidates: list[dict[str, Any]] = []
    seen = list(existing_texts)
    for payload in payloads:
        normalized = normalize_payload(payload)
        if normalized is None:
            stats["structure"] += 1
        elif not language_matches(normalized, locale):
            stats["language"] += 1
        elif is_near_duplicate(normalized["text"], seen):
            stats["duplicate"] += 1
        else:
            candidates.append(normalized)
            seen.append(normalized["text"])
    if not candidates:
        return [], stats
    verdicts = await judge_question_payloads(
        skill_label=skill_label,
        competency_label=competency_label,
        difficulty=difficulty,
        locale=locale,
        questions=candidates,
    )
    by_index = {verdict["index"]: verdict for verdict in verdicts if isinstance(verdict.get("index"), int)}
    accepted = []
    for index, question in enumerate(candidates):
        if _verdict_passes(by_index.get(index)):
            accepted.append(question)
        else:
            stats["judge"] += 1
    return accepted, stats


async def _existing_question_texts(db: AsyncSession, concept_id: str) -> list[str]:
    rows = await db.execute(
        text(
            "SELECT DISTINCT qv.canonical_text FROM interview_question_versions qv "
            "JOIN interview_questions q ON q.id = qv.question_id AND q.retired_at IS NULL "
            "JOIN question_version_taxonomy_concepts m ON m.question_version_id = qv.id "
            "WHERE m.concept_id = :concept_id"
        ),
        {"concept_id": concept_id},
    )
    return [str(row[0]) for row in rows.all() if row[0]]


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
    job_role: str | None,
    difficulty: str,
    locale: str,
    count: int,
    max_count: int = MAX_GENERATED_PER_TARGET,
) -> int:
    """Generate up to ``count`` gated drafts for one skill and commit them. Returns how many were filed."""
    if not get_settings().question_generation_enabled or count <= 0:
        return 0
    count = min(count, max_count)
    difficulty = difficulty if difficulty in {"foundational", "intermediate", "advanced"} else "intermediate"
    taxonomy = await load_active_skill_taxonomy(db)
    if concept_id not in taxonomy.skills:
        return 0
    competency = await _competency_for(db, taxonomy.version, concept_id, job_role)
    if competency is None:
        return 0
    competency_label = str(
        await db.scalar(
            text("SELECT label FROM taxonomy_concepts WHERE taxonomy_version = :v AND concept_id = :c"),
            {"v": taxonomy.version, "c": competency},
        )
        or competency
    )
    # The taxonomy label, never the plan's: nothing that came from a JD
    # reaches a prompt whose output other candidates will see.
    skill_label = taxonomy.skills[concept_id][0]
    existing_texts = await _existing_question_texts(db, concept_id)

    accepted: list[dict[str, Any]] = []
    rejected = {"structure": 0, "language": 0, "duplicate": 0, "judge": 0}
    for _attempt in range(_GATE_ATTEMPTS):
        missing = count - len(accepted)
        if missing <= 0:
            break
        try:
            payloads = await generate_question_payloads(
                skill_label=skill_label,
                competency_label=competency_label,
                difficulty=difficulty,
                locale=locale,
                count=missing,
            )
            batch, stats = await gate_questions(
                payloads=payloads[:missing],
                existing_texts=existing_texts + [item["text"] for item in accepted],
                skill_label=skill_label,
                competency_label=competency_label,
                difficulty=difficulty,
                locale=locale,
            )
        except Exception as exc:  # provider down, bad JSON: fail closed, file nothing more
            logger.warning("Question generation or gate failed for %s: %s", concept_id, exc)
            break
        accepted += batch
        for reason, value in stats.items():
            rejected[reason] += value

    filed = 0
    slug = concept_id.removeprefix("skill-")
    for normalized in accepted[:count]:
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
    trace_event(
        "interviewer",
        "question_generated",
        concept_id=concept_id,
        requested=count,
        filed=filed,
        rejected=rejected,
    )
    return filed


async def ensure_generated_questions(
    db: AsyncSession,
    *,
    target: dict[str, Any],
    job_concepts: set[str],
    job_role: str | None,
    difficulty: str,
    locale: str,
    desired: int,
    max_count: int = MAX_GENERATED_PER_TARGET,
) -> int:
    """Generate drafts until selection can reach ``desired`` questions for ``target``.

    Count and generate run under a transaction-scoped advisory lock per
    (skill, difficulty, locale): ingestion, publishing and a live session can
    reach the same skill at once, and without the lock each would generate its
    own batch. The transaction ends (commit) before returning, releasing it.
    """
    from src.modules.interviews.planning.question_selector import reachable_candidates

    if not get_settings().question_generation_enabled or desired <= 0:
        return 0
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"question-generation:{target['conceptId']}:{difficulty}:{locale.lower()}"},
    )
    try:
        available = len(
            await reachable_candidates(
                db,
                target=target,
                locale=locale,
                difficulty=difficulty,
                job_concepts=job_concepts,
                job_role=job_role,
                needed=desired,
            )
        )
        filed = 0
        if available < desired:
            filed = await generate_and_file(
                db,
                concept_id=target["conceptId"],
                job_role=job_role,
                difficulty=difficulty,
                locale=locale,
                count=desired - available,
                max_count=max_count,
            )
    finally:
        await db.commit()
    return filed
