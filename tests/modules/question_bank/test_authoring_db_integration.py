"""End-to-end: an authored question becomes selectable by the P2 selector.

Runs author -> rubric -> submit -> review -> approve -> `_load_candidates`
against real PostgreSQL inside a single transaction that is rolled back, so it
leaves no rows behind in the target database.
"""

import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database import engine
from src.modules.interviews.planning.question_selector import _load_candidates
from src.modules.question_bank.router import get_question
from src.modules.question_bank.schemas import (
    DEFAULT_TAXONOMY_VERSION,
    AttachRubricRequest,
    CreateQuestionDraftRequest,
    ReviewRequest,
)
from src.modules.question_bank.service import QuestionBankService

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_INTEGRATION_TESTS") != "1",
    reason="PostgreSQL integration database is not enabled",
)


@pytest.fixture(autouse=True)
async def _fresh_pool():
    """Each test runs on its own event loop; pooled connections must not cross loops."""
    await engine.dispose()
    yield
    await engine.dispose()


def _criterion(key: str, weight: str) -> dict:
    return {
        "stableKey": key,
        "name": key.title(),
        "description": f"Assesses {key}.",
        "weight": weight,
        "anchors": [{"level": level, "description": f"Level {level}"} for level in range(4)],
    }


@pytest.mark.asyncio
async def test_authored_question_reaches_selector_for_skill_and_career_targets() -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            db = AsyncSession(bind=connection, expire_on_commit=False)
            concepts = await db.execute(
                text(
                    "SELECT concept_id, kind FROM taxonomy_concepts "
                    "WHERE taxonomy_version = :version AND is_active AND kind IN ('competency', 'skill') "
                    "ORDER BY concept_id"
                ),
                {"version": DEFAULT_TAXONOMY_VERSION},
            )
            by_kind: dict[str, str] = {}
            for row in concepts.mappings():
                by_kind.setdefault(row["kind"], row["concept_id"])
            if {"competency", "skill"} - by_kind.keys():
                pytest.skip("Taxonomy seed is missing; run the application seeds first.")
            competency, skill = by_kind["competency"], by_kind["skill"]

            author, reviewer, approver = (str(uuid4()) for _ in range(3))
            service = QuestionBankService(db)
            draft = await service.create_draft(
                CreateQuestionDraftRequest.model_validate({
                    "stableKey": f"it.authoring.{uuid4().hex[:12]}",
                    "taxonomyVersion": DEFAULT_TAXONOMY_VERSION,
                    "questionType": "TECHNICAL",
                    "difficultyBand": "Intermediate",
                    "canonicalLocale": "vi-VN",
                    "canonicalText": "Giải thích cơ chế hoạt động của connection pool.",
                    "objective": "Hiểu vòng đời kết nối và giới hạn tài nguyên.",
                    "thinkingSeconds": 30,
                    "softAnswerSeconds": 150,
                    "hardAnswerSeconds": 240,
                    "taxonomyMappings": [
                        {"conceptId": competency, "purpose": "PRIMARY_COMPETENCY", "relevance": 1},
                        {"conceptId": skill, "purpose": "TARGET_SKILL", "relevance": 1},
                        {"conceptId": competency, "purpose": "TARGET_ROLE", "relevance": 1},
                    ],
                }),
                author,
            )
            await service.attach_rubric(
                draft.id,
                author,
                AttachRubricRequest.model_validate({
                    # 0.1 + 0.2 + 0.7 is not exactly 1.0 in floating point.
                    "criteria": [
                        _criterion("accuracy", "0.1"),
                        _criterion("depth", "0.2"),
                        _criterion("tradeoffs", "0.7"),
                    ],
                }),
            )
            await service.submit(draft.id, author)
            approve_review = ReviewRequest.model_validate({"reviewType": "CONTENT", "decision": "APPROVE"})
            await service.record_review(draft.id, reviewer, approve_review)
            approved = await service.approve(draft.id, approver, "qb-approval-v1")
            assert approved.status == "APPROVED"

            # P1 stamps parser targets with `internal-2026.1`; the selector bridges it.
            skill_target = {"conceptId": skill, "taxonomyVersion": "internal-2026.1", "rationale": {}}
            career_target = {
                "conceptId": competency,
                "taxonomyVersion": DEFAULT_TAXONOMY_VERSION,
                "rationale": {"source": "career_classification_fallback"},
            }
            # A cloned taxonomy version keeps the same concept ids.
            cloned_target = {"conceptId": skill, "taxonomyVersion": "internal-2026.2", "rationale": {}}
            for target, purpose in (
                (skill_target, "TARGET_SKILL"),
                (career_target, "TARGET_ROLE"),
                (cloned_target, "TARGET_SKILL"),
            ):
                candidates = await _load_candidates(
                    db, target=target, locale="vi-VN", difficulty="intermediate", salt="it"
                )
                selected = next(c for c in candidates if c.question_version_id == str(draft.id))
                assert selected.mapping_purpose == purpose
                assert selected.question_type == "technical"
                assert sum(Decimal(str(c["weight"])) for c in selected.rubric["criteria"]) == 1

            # The admin detail page drives the lifecycle buttons from this read model.
            detail = await get_question(question_id=approved.question_id, _={}, db=db)
            assert detail["currentVersion"]["status"] == "APPROVED"
            assert detail["currentVersion"]["objective"]
            assert detail["currentVersion"]["createdBy"] == author
            assert [item["conceptId"] for item in detail["taxonomy"]["skills"]] == [skill]
            assert [item["conceptId"] for item in detail["taxonomy"]["roles"]] == [competency]
            assert detail["rubric"]["approved"] is True
            criteria_keys = [c["stableKey"] for c in detail["rubric"]["criteria"]]
            assert criteria_keys == ["accuracy", "depth", "tradeoffs"]
        finally:
            await transaction.rollback()


class _Upload:
    filename = "questions.csv"

    def __init__(self, content: bytes) -> None:
        self.content = content

    async def read(self, size: int = -1) -> bytes:
        return self.content[:size] if size >= 0 else self.content


def _csv(rows: list[dict]) -> bytes:
    import csv
    import io

    from src.modules.question_bank.import_parser import IMPORT_COLUMNS

    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=IMPORT_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return stream.getvalue().encode("utf-8")


@pytest.mark.asyncio
async def test_import_commit_writes_selector_mappings_and_rejects_duplicates() -> None:
    import json

    from fastapi import HTTPException

    from src.modules.question_bank.import_service import QuestionImportService

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            db = AsyncSession(bind=connection, expire_on_commit=False)
            rows = (
                await db.execute(
                    text(
                        "SELECT concept_id, kind FROM taxonomy_concepts WHERE taxonomy_version = :v "
                        "AND is_active AND kind IN ('competency', 'skill') ORDER BY concept_id"
                    ),
                    {"v": DEFAULT_TAXONOMY_VERSION},
                )
            ).mappings().all()
            by_kind: dict[str, str] = {}
            for row in rows:
                by_kind.setdefault(row["kind"], row["concept_id"])
            if {"competency", "skill"} - by_kind.keys():
                pytest.skip("Taxonomy seed is missing; run the application seeds first.")

            # A rubric to reference by key.
            author = str(uuid4())
            service = QuestionBankService(db)
            seed_draft = await service.create_draft(
                CreateQuestionDraftRequest.model_validate({
                    "stableKey": f"it.rubric-host.{uuid4().hex[:8]}",
                    "taxonomyVersion": DEFAULT_TAXONOMY_VERSION,
                    "questionType": "technical", "difficultyBand": "intermediate",
                    "canonicalLocale": "vi-VN", "canonicalText": "Q", "objective": "O",
                    "softAnswerSeconds": 60, "hardAnswerSeconds": 120,
                    "taxonomyMappings": [
                        {"conceptId": by_kind["competency"], "purpose": "PRIMARY_COMPETENCY", "relevance": 1},
                    ],
                }),
                author,
            )
            rubric_version = await service.attach_rubric(
                seed_draft.id, author,
                AttachRubricRequest.model_validate({"criteria": [_criterion("accuracy", "1")]}),
            )
            rubric_key = (
                await db.execute(
                    text("SELECT r.stable_key FROM rubrics r WHERE r.current_version_id = :id"),
                    {"id": rubric_version.id},
                )
            ).scalar_one()

            def row(key: str) -> dict:
                return {
                    "stable_key": key, "version": "1.0.0", "canonical_locale": "vi-VN",
                    "canonical_text": "Câu hỏi import", "objective": "Mục tiêu",
                    "question_type": "technical", "difficulty_band": "intermediate",
                    "thinking_seconds": "30", "soft_answer_seconds": "150", "hard_answer_seconds": "240",
                    "taxonomy_version": "", "target_role_ids": json.dumps([by_kind["competency"]]),
                    "primary_competency_id": by_kind["competency"], "supporting_competency_ids": "[]",
                    "skill_ids": json.dumps([by_kind["skill"]]), "expected_points": "[]",
                    "source_refs": "[]", "rubric_key": rubric_key, "change_summary": "",
                }

            importer = QuestionImportService(db)
            key = f"it.import.{uuid4().hex[:8]}"
            run = await importer.create_csv_import(_Upload(_csv([row(key)])), author)
            assert run.valid_rows == 1, (await importer.rows(run.id))[0].validation_errors
            committed = await importer.commit(run.id, author, "it-key-1")
            version_id = committed[0].draft_question_version_id
            purposes = set(
                (
                    await db.execute(
                        text(
                            "SELECT purpose FROM question_version_taxonomy_concepts "
                            "WHERE question_version_id = :id"
                        ),
                        {"id": version_id},
                    )
                ).scalars()
            )
            assert purposes == {"PRIMARY_COMPETENCY", "TARGET_SKILL", "TARGET_ROLE"}
            linked = await db.scalar(
                text("SELECT rubric_version_id FROM question_version_rubrics WHERE question_version_id = :id"),
                {"id": version_id},
            )
            assert linked == rubric_version.id

            # Same key again (already in bank) and twice in one file -> 409, not a 500.
            dup = await importer.create_csv_import(_Upload(_csv([row(key), row(key + ".b"), row(key + ".b")])), author)
            with pytest.raises(HTTPException) as excinfo:
                await importer.commit(dup.id, author, "it-key-2")
            assert excinfo.value.status_code == 409
            assert excinfo.value.detail["alreadyInBank"] == [key]
            assert excinfo.value.detail["duplicatedInFile"] == [key + ".b"]
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_revision_replaces_the_approved_version_only_once_approved() -> None:
    """M15: an approved question can be revised; interviews switch only on approval."""
    from src.modules.question_bank.schemas import UpdateQuestionDraftRequest

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            db = AsyncSession(bind=connection, expire_on_commit=False)
            rows = (
                await db.execute(
                    text(
                        "SELECT concept_id, kind FROM taxonomy_concepts WHERE taxonomy_version = :v "
                        "AND is_active AND kind IN ('competency', 'skill') ORDER BY concept_id"
                    ),
                    {"v": DEFAULT_TAXONOMY_VERSION},
                )
            ).mappings().all()
            by_kind: dict[str, str] = {}
            for row in rows:
                by_kind.setdefault(row["kind"], row["concept_id"])
            if {"competency", "skill"} - by_kind.keys():
                pytest.skip("Taxonomy seed is missing; run the application seeds first.")

            author, reviewer = str(uuid4()), str(uuid4())
            service = QuestionBankService(db)
            review = ReviewRequest.model_validate({"reviewType": "CONTENT", "decision": "APPROVE"})

            async def approve(version_id):
                await service.submit(version_id, author)
                await service.record_review(version_id, reviewer, review)
                return await service.approve(version_id, reviewer, "qb-approval-v1")

            v1 = await service.create_draft(
                CreateQuestionDraftRequest.model_validate({
                    "stableKey": f"it.revise.{uuid4().hex[:8]}",
                    "taxonomyVersion": DEFAULT_TAXONOMY_VERSION,
                    "questionType": "technical", "difficultyBand": "intermediate",
                    "canonicalLocale": "vi-VN", "canonicalText": "Bản 1", "objective": "O",
                    "thinkingSeconds": 30, "softAnswerSeconds": 150, "hardAnswerSeconds": 180,
                    "taxonomyMappings": [
                        {"conceptId": by_kind["competency"], "purpose": "PRIMARY_COMPETENCY", "relevance": 1},
                        {"conceptId": by_kind["skill"], "purpose": "TARGET_SKILL", "relevance": 1},
                    ],
                }),
                author,
            )
            await service.attach_rubric(
                v1.id, author, AttachRubricRequest.model_validate({"criteria": [_criterion("accuracy", "1")]})
            )
            await approve(v1.id)

            # Reviewer cannot review their own work.
            with pytest.raises(Exception, match="cannot review"):
                await service.record_review(v1.id, author, review)

            v2 = await service.revise(v1.id, author)
            assert (v2.version, v2.status) == ("1.0.1", "DRAFT")
            with pytest.raises(Exception, match="in progress"):
                await service.revise(v1.id, author)
            await service.update_draft(
                v2.id, author, UpdateQuestionDraftRequest.model_validate({"canonicalText": "Bản 2"})
            )

            target = {"conceptId": by_kind["skill"], "taxonomyVersion": "internal-2026.1", "rationale": {}}
            texts = {
                c.question_version_id: c.question_text
                for c in await _load_candidates(db, target=target, locale="vi-VN", difficulty="intermediate")
            }
            assert texts.get(str(v1.id)) == "Bản 1" and str(v2.id) not in texts

            await approve(v2.id)
            texts = {
                c.question_version_id: c.question_text
                for c in await _load_candidates(db, target=target, locale="vi-VN", difficulty="intermediate")
            }
            assert texts.get(str(v2.id)) == "Bản 2" and str(v1.id) not in texts
        finally:
            await transaction.rollback()
