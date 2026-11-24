"""Fallback ladder for skills the Question Bank does not cover.

Unit tests patch the database helpers; the DB-backed tests (opt-in with
RUN_DB_INTEGRATION_TESTS=1) run the real SQL inside a rolled-back transaction.
"""

import os
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.interviews.planning import question_generation, question_selector
from tests.modules.interviews.test_question_selector import candidate


def _payload(weights=(0.5, 0.5)) -> dict:
    return {
        "text": "How would you keep a Node.js API responsive under CPU-heavy work?",
        "objective": "Assess event-loop understanding.",
        "criteria": [
            {
                "stableKey": f"Criterion {index}",
                "name": f"Criterion {index}",
                "description": "What a good answer covers.",
                "weight": weight,
                "anchors": [{"level": level, "description": f"Level {level}"} for level in range(4)],
            }
            for index, weight in enumerate(weights)
        ],
    }


def test_generated_payload_weights_are_renormalised_to_one() -> None:
    normalized = question_generation.normalize_payload(_payload(weights=(0.33, 0.33, 0.33)))

    assert normalized is not None
    assert sum(Decimal(str(item["weight"])) for item in normalized["criteria"]) == Decimal(1)
    assert normalized["criteria"][0]["stableKey"] == "criterion-0"


@pytest.mark.parametrize(
    "broken",
    [
        {"text": "", "objective": "x", "criteria": _payload()["criteria"]},
        {**_payload(), "criteria": []},
        {**_payload(), "criteria": [{**_payload()["criteria"][0], "anchors": []}]},
        _payload(weights=(0.0, 1.0)),
    ],
)
def test_invalid_generated_payload_is_rejected(broken: dict) -> None:
    assert question_generation.normalize_payload(broken) is None


@pytest.mark.asyncio
async def test_role_questions_count_only_when_their_skill_is_in_the_jd(monkeypatch) -> None:
    """A backend Java question is no substitute for Node.js in a JD without Java."""

    async def broader(db, concept_id):
        return []

    async def roles(db, concept_id, job_role):
        return ["technology.software-engineering.backend"]

    async def load(db, *, target, generated=False, **kwargs):
        if generated:
            return []
        return [
            candidate(question_version_id="java-q", mapping_purpose="TARGET_ROLE"),
            candidate(question_version_id="sql-q", mapping_purpose="TARGET_ROLE"),
        ]

    async def skills(db, question_version_id):
        return {"java-q": {"skill-java"}, "sql-q": {"skill-sql"}}[question_version_id]

    monkeypatch.setattr(question_selector, "_broader_concepts", broader)
    monkeypatch.setattr(question_selector, "_role_concepts", roles)
    monkeypatch.setattr(question_selector, "_load_candidates", load)
    monkeypatch.setattr(question_selector, "_question_skill_concepts", skills)

    found = await question_selector._fallback_candidates(
        None,
        target={"conceptId": "skill-nodejs", "taxonomyVersion": "v", "label": "Node.js"},
        locale="en-US",
        difficulty="intermediate",
        salt="s",
        job_concepts={"skill-nodejs", "skill-sql"},
        job_role="technology.software-engineering.backend",
    )

    assert [(item.question_version_id, item.source) for item in found] == [("sql-q", "role")]


@pytest.mark.asyncio
async def test_ladder_order_is_broader_then_role_then_generated(monkeypatch) -> None:
    async def broader(db, concept_id):
        return ["skill-javascript"]

    async def roles(db, concept_id, job_role):
        return ["technology.software-engineering.backend"]

    async def load(db, *, target, generated=False, **kwargs):
        if generated:
            return [candidate(question_version_id="gen-q", source="generated_unreviewed")]
        if target["conceptId"] == "skill-javascript":
            return [candidate(question_version_id="js-q")]
        return [candidate(question_version_id="role-q", mapping_purpose="TARGET_ROLE")]

    async def skills(db, question_version_id):
        return {"skill-sql"}

    monkeypatch.setattr(question_selector, "_broader_concepts", broader)
    monkeypatch.setattr(question_selector, "_role_concepts", roles)
    monkeypatch.setattr(question_selector, "_load_candidates", load)
    monkeypatch.setattr(question_selector, "_question_skill_concepts", skills)

    found = await question_selector._fallback_candidates(
        None,
        target={"conceptId": "skill-nodejs", "taxonomyVersion": "v", "label": "Node.js"},
        locale="en-US",
        difficulty="intermediate",
        salt="s",
        job_concepts={"skill-sql"},
        job_role=None,
    )

    assert [(item.question_version_id, item.source) for item in found] == [
        ("js-q", "broader_skill"),
        ("role-q", "role"),
        ("gen-q", "generated_unreviewed"),
    ]


_db = pytest.mark.skipif(
    os.getenv("RUN_DB_INTEGRATION_TESTS") != "1",
    reason="PostgreSQL integration database is not enabled",
)


@pytest.fixture
async def rollback_db():
    from src.infrastructure.database import engine

    await engine.dispose()
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield AsyncSession(bind=connection, expire_on_commit=False)
        finally:
            await transaction.rollback()
    await engine.dispose()


@_db
@pytest.mark.asyncio
async def test_broader_concepts_follow_the_seeded_catalogue(rollback_db) -> None:
    assert (await question_selector._broader_concepts(rollback_db, "skill-mysql"))[0] == "skill-sql"
    chain = await question_selector._broader_concepts(rollback_db, "skill-large-language-models")
    assert chain[:2] == ["skill-generative-ai", "skill-artificial-intelligence"]


@_db
@pytest.mark.asyncio
async def test_generated_question_is_filed_for_review_and_reused(rollback_db, monkeypatch) -> None:
    calls = []

    async def fake_payloads(**kwargs):
        calls.append(kwargs)
        return [_payload()]

    monkeypatch.setattr(question_generation, "generate_question_payloads", fake_payloads)

    filed = await question_generation.generate_and_file(
        rollback_db,
        concept_id="skill-php",
        job_role="technology.software-engineering.backend",
        difficulty="unspecified",
        locale="en-US",
        count=5,
    )

    # The fake returns the same question every time: the gate's regeneration
    # round sees it as a duplicate, so only one is filed.
    assert filed == 1
    # Only the concept reaches the model, never JD text; the count is capped.
    assert calls[0] == {
        "skill_label": "PHP",
        "competency_label": "Backend Engineering",
        "difficulty": "intermediate",
        "locale": "en-US",
        "count": question_generation.MAX_GENERATED_PER_TARGET,
    }
    assert [call["count"] for call in calls[1:]] == [1]
    status = await rollback_db.scalar(
        text(
            "SELECT v.status FROM interview_question_versions v "
            "JOIN question_version_taxonomy_concepts m ON m.question_version_id = v.id "
            "WHERE m.concept_id = 'skill-php' AND v.created_by = :author"
        ),
        {"author": "system-question-generator"},
    )
    assert status == "IN_REVIEW"

    reused = await question_selector._load_candidates(
        rollback_db,
        target={"conceptId": "skill-php", "taxonomyVersion": "internal-2026.1", "rationale": {}},
        locale="en-US",
        difficulty="intermediate",
        generated=True,
    )
    assert [item.source for item in reused] == ["generated_unreviewed"]
    # Approved-only loading never serves an unreviewed draft.
    assert (
        await question_selector._load_candidates(
            rollback_db,
            target={"conceptId": "skill-php", "taxonomyVersion": "internal-2026.1", "rationale": {}},
            locale="en-US",
            difficulty="intermediate",
        )
        == []
    )


@_db
@pytest.mark.asyncio
async def test_generation_disabled_files_nothing(rollback_db, monkeypatch) -> None:
    from src.core.config import get_settings

    monkeypatch.setattr(get_settings(), "question_generation_enabled", False)
    assert (
        await question_generation.generate_and_file(
            rollback_db,
            concept_id="skill-php",
            job_role=None,
            difficulty="intermediate",
            locale="en-US",
            count=1,
        )
        == 0
    )
