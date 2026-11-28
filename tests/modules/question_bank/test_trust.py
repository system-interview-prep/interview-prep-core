"""Trust tiers: how far a question version has been checked, and who may verify it."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from src.modules.interviews.agents.planner.selection import _candidate_rank
from src.modules.question_bank.permissions import version_actions
from src.modules.question_bank.service import QuestionBankService
from src.modules.question_bank.trust import (
    CALIBRATION_MIN_SAMPLE,
    tier_name,
    trust_summary,
    trust_tier_sql,
    trust_tiers,
)
from tests.modules.interviews.test_question_fallback import _db, rollback_db  # noqa: F401
from tests.modules.interviews.test_question_selector import candidate


def test_tier_names_and_summary() -> None:
    assert [tier_name(tier) for tier in (0, 1, 2, 3)] == ["generated", "initial", "reviewed", "calibrated"]
    # An unknown or missing tier is never reported as checked.
    assert tier_name(None) == "generated" and tier_name(9) == "generated"
    assert trust_summary(["initial", "reviewed", "calibrated", "generated"]) == {"total": 4, "reviewed": 2}
    assert trust_summary([]) == {"total": 0, "reviewed": 0}


def test_tier_sql_needs_no_bound_parameter() -> None:
    sql = trust_tier_sql("v")
    assert ":" not in sql and "%" not in sql
    assert "v.status" in sql and f">= {CALIBRATION_MIN_SAMPLE}" in sql


def _verify(status="APPROVED", created_by="other", roles=("ADMIN",), tier=1):
    return version_actions(
        status=status, created_by=created_by, actor_id="me", roles=roles, has_rubric=True,
        approve_reviews=0, trust_tier=tier,
    )["verify"]


def test_only_an_admin_verifies_an_approved_version_nobody_reviewed() -> None:
    assert _verify()["allowed"]
    assert _verify(roles=("QUESTION_AUTHOR",))["reason"] == "no_role"
    assert _verify(status="IN_REVIEW", tier=0)["reason"] == "not_approved"
    assert _verify(tier=2)["reason"] == "already_reviewed"
    assert _verify(created_by="me")["reason"] == "own_question"


def test_selection_prefers_the_question_checked_further() -> None:
    seeded = candidate(question_version_id="seeded", trust_tier=1)
    reviewed = candidate(question_version_id="reviewed", trust_tier=2)

    def rank(item, exposure=None):
        return _candidate_rank(item, difficulty="foundational", locale="en-US", exposure=exposure)

    assert sorted([seeded, reviewed], key=rank)[0].question_version_id == "reviewed"
    # Rotation still wins: a seeded question the candidate has not met goes
    # before a reviewed one they already answered.
    seen = {"reviewed": "2026-11-01T00:00:00"}
    ranked = sorted([reviewed, seeded], key=lambda item: rank(item, seen))
    assert ranked[0].question_version_id == "seeded"
    # Difficulty outranks trust: the right level is not traded for a review.
    harder = candidate(question_version_id="harder", trust_tier=3, difficulty_band="advanced")
    assert sorted([harder, seeded], key=rank)[0].question_version_id == "seeded"


@_db
@pytest.mark.asyncio
async def test_review_and_calibration_move_a_seeded_question_up(rollback_db) -> None:  # noqa: F811
    row = (
        await rollback_db.execute(
            text(
                "SELECT CAST(qv.id AS TEXT) AS version_id, CAST(q.id AS TEXT) AS question_id "
                "FROM interview_questions q JOIN interview_question_versions qv "
                "ON qv.id = q.current_approved_version_id "
                "WHERE q.retired_at IS NULL AND qv.created_by = 'system-seed' "
                "AND NOT EXISTS (SELECT 1 FROM question_reviews r WHERE r.question_version_id = qv.id) "
                "AND NOT EXISTS (SELECT 1 FROM question_calibrations c WHERE c.question_version_id = qv.id) "
                "AND NOT EXISTS (SELECT 1 FROM question_bank_suggestions s WHERE s.question_id = q.id "
                "AND s.status = 'OPEN') LIMIT 1"
            )
        )
    ).mappings().one_or_none()
    if row is None:
        pytest.skip("no untouched seeded question in this database")
    version_id, question_id = row["version_id"], row["question_id"]

    async def tier() -> str:
        return (await trust_tiers(rollback_db, [version_id]))[version_id]

    assert await tier() == "initial"

    service = QuestionBankService(rollback_db)
    admin = str(uuid4())
    with pytest.raises(HTTPException) as own:
        await service.verify(version_id, "system-seed")
    assert own.value.status_code == 403

    review = await service.verify(version_id, admin, "Đã đọc và đồng ý.")
    assert review.review_type == "VERIFICATION" and review.decision == "APPROVE"
    assert await tier() == "reviewed"
    with pytest.raises(HTTPException) as again:
        await service.verify(version_id, str(uuid4()))
    assert again.value.status_code == 409

    async def calibrate(sample: int) -> None:
        await rollback_db.execute(
            text(
                "INSERT INTO question_calibrations (id, question_version_id, calibration_version, "
                "sample_size, mean_score, score_stddev, mean_answer_seconds, timeout_rate, skip_rate, "
                "clarification_rate) "
                "VALUES (:id, CAST(:version_id AS uuid), :label, :sample, 0.6, 0.2, 0, 0, 0, 0)"
            ),
            {"id": uuid4(), "version_id": version_id, "label": f"test-{sample}", "sample": sample},
        )

    await calibrate(CALIBRATION_MIN_SAMPLE - 1)
    assert await tier() == "reviewed"
    await calibrate(CALIBRATION_MIN_SAMPLE)
    assert await tier() == "calibrated"

    # A finding from real interviews reopens the question until someone deals with it.
    await rollback_db.execute(
        text(
            "INSERT INTO question_bank_suggestions (id, kind, reason, question_id, dedupe_key) "
            "VALUES (:id, 'REVISE', 'test', CAST(:question_id AS uuid), :key)"
        ),
        {"id": uuid4(), "question_id": question_id, "key": f"test-{uuid4()}"},
    )
    assert await tier() == "reviewed"


@_db
@pytest.mark.asyncio
async def test_a_draft_cannot_be_verified(rollback_db) -> None:  # noqa: F811
    version_id = await rollback_db.scalar(
        text("SELECT id FROM interview_question_versions WHERE status = 'IN_REVIEW' LIMIT 1")
    )
    if version_id is None:
        pytest.skip("no version in review in this database")
    with pytest.raises(HTTPException) as raised:
        await QuestionBankService(rollback_db).verify(version_id, str(uuid4()))
    assert raised.value.status_code == 409
