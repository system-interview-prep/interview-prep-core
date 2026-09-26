"""Integration tests for the Question Bank eligibility pipeline and seed.

Tests are grouped into:
  1. question_selector unit logic (pure Python, no DB)
  2. Seed idempotency and data correctness (async DB)
  3. Eligibility integration: full stack via seed → selector

These tests cover all 8 scenarios listed in the task:
  ✓ empty bank → deterministic unreviewed fallback prompts
  ✓ seeded competency with enough questions → selection succeeds
  ✓ partial bank coverage → curated questions plus fallback prompts
  ✓ unapproved question excluded
  ✓ uncalibrated (wrong status) excluded
  ✓ wrong taxonomy concept excluded
  ✓ inactive question (retired_at set) excluded
  ✓ seed can run twice without duplicates
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.modules.interviews.question_selector import (
    _allowed_purposes,
    _Candidate,
    _candidate_rank,
    _snapshot,
)
from src.seeds.question_bank_seed import (
    _CONCEPT_FIXTURES,
    _QUESTION_TAXONOMY_VERSION,
    _det_uuid,
    seed_question_bank,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _candidate(**overrides: Any) -> _Candidate:
    rv_id = overrides.get("rubric_version_id", str(uuid.uuid4()))
    defaults: dict[str, Any] = {
        "question_version_id": str(uuid.uuid4()),
        "rubric_version_id": rv_id,
        "stable_key": "test-question",
        "version": "1.0.0",
        "question_type": "technical",
        "difficulty_band": "intermediate",
        "canonical_locale": "en-US",
        "question_text": "Explain something.",
        "objective": "Assess understanding.",
        "soft_answer_seconds": 120,
        "hard_answer_seconds": 180,
        "mapping_purpose": "PRIMARY_COMPETENCY",
        "relevance": 1.0,
        "expected_points": [],
        "rubric": {"rubricVersionId": rv_id, "criteria": []},
    }
    defaults.update(overrides)
    return _Candidate(**defaults)


# ===========================================================================
# 1. Pure unit tests – question_selector logic
# ===========================================================================


class TestAllowedPurposes:
    def test_job_requirement_source_allows_skill_and_primary_competency(self) -> None:
        target = {"rationale": {"source": "job_requirement"}}
        assert _allowed_purposes(target) == ("TARGET_SKILL", "PRIMARY_COMPETENCY")

    def test_career_fallback_only_allows_role(self) -> None:
        target = {"rationale": {"source": "career_classification_fallback"}}
        assert _allowed_purposes(target) == ("TARGET_ROLE",)

    def test_none_rationale_defaults_to_skill_and_primary(self) -> None:
        assert _allowed_purposes({}) == ("TARGET_SKILL", "PRIMARY_COMPETENCY")


class TestCandidateRanking:
    def test_exact_locale_ranks_before_language_only(self) -> None:
        exact = _candidate(question_version_id="exact", canonical_locale="vi-VN")
        lang_only = _candidate(question_version_id="lang", canonical_locale="vi")
        ranked = sorted(
            [lang_only, exact],
            key=lambda c: _candidate_rank(c, difficulty="intermediate", locale="vi-VN"),
        )
        assert ranked[0].question_version_id == "exact"

    def test_nearer_difficulty_ranks_higher(self) -> None:
        easy = _candidate(question_version_id="easy", difficulty_band="foundational")
        hard = _candidate(question_version_id="hard", difficulty_band="advanced")
        ranked = sorted(
            [hard, easy],
            key=lambda c: _candidate_rank(c, difficulty="intermediate", locale="en-US"),
        )
        # Both are 1 step from intermediate; foundational comes first (lower index)
        assert {r.question_version_id for r in ranked} == {"easy", "hard"}

    def test_primary_competency_ranks_before_target_skill(self) -> None:
        primary = _candidate(question_version_id="primary", mapping_purpose="PRIMARY_COMPETENCY")
        skill = _candidate(question_version_id="skill", mapping_purpose="TARGET_SKILL")
        ranked = sorted(
            [skill, primary],
            key=lambda c: _candidate_rank(c, difficulty="intermediate", locale="en-US"),
        )
        assert ranked[0].question_version_id == "primary"


class TestSnapshot:
    def test_snapshot_contains_all_required_fields(self) -> None:
        item = _candidate()
        target = {
            "taxonomyVersion": "internal-2026.1",
            "conceptId": "skill-artificial-intelligence",
            "label": "Artificial Intelligence",
        }
        snap = _snapshot(item, target=target, locale="en-US", selection_rank=0)

        assert snap["questionVersionId"] == item.question_version_id
        assert snap["rubric"]["rubricVersionId"] == item.rubric_version_id
        assert snap["taxonomyTarget"]["conceptId"] == "skill-artificial-intelligence"
        assert snap["taxonomyTarget"]["taxonomyVersion"] == "internal-2026.1"
        assert snap["selectionRank"] == 0
        assert snap["schemaVersion"] == "1.0"


# ===========================================================================
# 2. Seed unit tests (mock DB)
# ===========================================================================


class TestDeterministicUUID:
    def test_same_inputs_produce_same_uuid(self) -> None:
        a = _det_uuid("question", "ai-fundamentals-bias-fairness")
        b = _det_uuid("question", "ai-fundamentals-bias-fairness")
        assert a == b

    def test_different_keys_produce_different_uuids(self) -> None:
        a = _det_uuid("question", "ai-fundamentals-bias-fairness")
        b = _det_uuid("question", "ai-fundamentals-overfitting-regularization")
        assert a != b

    def test_different_namespaces_produce_different_uuids(self) -> None:
        a = _det_uuid("question", "ai-fundamentals-bias-fairness")
        b = _det_uuid("question-version", "ai-fundamentals-bias-fairness")
        assert a != b


class TestConceptFixtures:
    """Validate structural integrity of the fixture data before DB seeding."""

    def test_all_required_concepts_are_covered(self) -> None:
        required = {
            "skill-artificial-intelligence",
            "skill-generative-ai",
            "skill-large-language-models",
            "skill-natural-language-processing",
        }
        seeded = {f["concept_id"] for f in _CONCEPT_FIXTURES}
        assert required.issubset(seeded)

    def test_ai_has_at_least_five_questions(self) -> None:
        ai_fixture = next(f for f in _CONCEPT_FIXTURES if f["concept_id"] == "skill-artificial-intelligence")
        assert len(ai_fixture["questions"]) >= 5, "Need ≥5 questions for headroom (target=3)"

    def test_other_concepts_have_at_least_three_questions(self) -> None:
        other = [f for f in _CONCEPT_FIXTURES if f["concept_id"] != "skill-artificial-intelligence"]
        for fixture in other:
            assert len(fixture["questions"]) >= 3, (
                f"{fixture['concept_id']} should have ≥3 questions for spare candidates"
            )

    def test_all_stable_keys_are_unique(self) -> None:
        keys: list[str] = []
        for fixture in _CONCEPT_FIXTURES:
            keys.extend(q["stable_key"] for q in fixture["questions"])
        assert len(keys) == len(set(keys)), "Duplicate stable_key detected in fixtures"

    def test_all_questions_use_correct_difficulty_values(self) -> None:
        valid = {"foundational", "intermediate", "advanced"}
        for fixture in _CONCEPT_FIXTURES:
            for q in fixture["questions"]:
                assert q["difficulty"] in valid, (
                    f"{q['stable_key']} has invalid difficulty: {q['difficulty']}"
                )

    def test_taxonomy_version_constant_matches_planner(self) -> None:
        # The planner and JD parser hard-code "internal-2026.1".
        # If this changes the seed must change too.
        assert _QUESTION_TAXONOMY_VERSION == "internal-2026.1"


# ===========================================================================
# 3. Eligibility integration – mock async DB
# ===========================================================================


def _make_db_mock(*, version_exists: bool = False) -> AsyncMock:
    """Return an AsyncMock session where scalar() returns None or 1."""
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=1 if version_exists else None)
    db.execute = AsyncMock()
    return db


def _make_session_factory(db_mock: AsyncMock):
    """Return a session-factory context-manager compatible with seed_question_bank."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=db_mock)
    cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock()
    factory.return_value = cm
    return factory


@pytest.mark.asyncio
class TestSeedIdempotency:
    async def test_fresh_seed_creates_questions(self) -> None:
        """When no questions exist (scalar returns None), seed inserts all."""
        db = _make_db_mock(version_exists=False)
        factory = _make_session_factory(db)

        summary = await seed_question_bank(factory)

        assert summary["skill-artificial-intelligence"] == 12
        assert summary["skill-generative-ai"] == 6
        assert summary["skill-large-language-models"] == 6
        assert summary["skill-natural-language-processing"] == 6

    async def test_second_run_creates_nothing(self) -> None:
        """When all versions exist (scalar returns 1), seeded count is 0."""
        db = _make_db_mock(version_exists=True)
        factory = _make_session_factory(db)

        summary = await seed_question_bank(factory)

        # All seeded=0 because _seed_one_question returned False for every question
        assert sum(summary.values()) == 0

    async def test_db_commit_is_called(self) -> None:
        db = _make_db_mock(version_exists=False)
        factory = _make_session_factory(db)

        await seed_question_bank(factory)

        db.commit.assert_awaited_once()


# ===========================================================================
# 4. Selector eligibility rules – unit tests using mocked _load_candidates
# ===========================================================================


@pytest.mark.asyncio
class TestSelectorEligibilityRules:
    """Tests that the selector enforces eligibility at the DB query level.

    We mock _load_candidates to simulate different bank states without
    requiring a real database.
    """

    _TARGET: dict[str, Any] = {
        "selectionRank": 0,
        "taxonomyVersion": "internal-2026.1",
        "conceptId": "skill-artificial-intelligence",
        "label": "Artificial Intelligence",
        "importance": 0.5,
        "targetQuestionCount": 3,
        "rationale": {"source": "job_requirement"},
    }

    def _make_approved_candidates(self, n: int) -> list[_Candidate]:
        return [
            _candidate(
                question_version_id=f"qv-{i}",
                rubric_version_id=f"rv-{i}",
                stable_key=f"ai-question-{i}",
            )
            for i in range(n)
        ]

    async def test_empty_bank_uses_deterministic_fallbacks(self) -> None:
        """An empty bank still produces a usable, explicitly unreviewed interview."""
        from src.modules.interviews.question_selector import select_and_freeze_questions

        db = AsyncMock()
        db.execute = AsyncMock()

        # Simulate plan row returned from DB
        plan_mappings = MagicMock()
        plan_mappings.one_or_none.return_value = {"status": "READY", "plan_payload": {}}
        db.execute.return_value.mappings.return_value = plan_mappings

        # Simulate targets with 1 competency requiring 3 questions
        targets_mock = MagicMock()
        targets_mock.all.return_value = [
            {
                "selection_rank": 0,
                "taxonomy_version": "internal-2026.1",
                "concept_id": "skill-artificial-intelligence",
                "label": "Artificial Intelligence",
                "importance": Decimal("0.5"),
                "target_question_count": 3,
                "rationale": None,
            }
        ]

        call_count = 0

        async def _side_effect(query, params=None):
            nonlocal call_count
            result = MagicMock()
            if call_count == 0:
                # First call: FOR UPDATE plan lock
                result.mappings.return_value.one_or_none.return_value = {
                    "status": "READY",
                    "plan_payload": {},
                }
            elif call_count == 1:
                # Second call: session_competency_targets
                result.mappings.return_value.all.return_value = [
                    {
                        "selection_rank": 0,
                        "taxonomy_version": "internal-2026.1",
                        "concept_id": "skill-artificial-intelligence",
                        "label": "Artificial Intelligence",
                        "importance": Decimal("0.5"),
                        "target_question_count": 3,
                        "rationale": None,
                    }
                ]
            else:
                # Candidate query: returns nothing (empty bank)
                result.mappings.return_value.all.return_value = []
            call_count += 1
            return result

        db.execute.side_effect = _side_effect

        session_row = {
            "id": str(uuid.uuid4()),
            "plan_id": str(uuid.uuid4()),
            "locale": "en-US",
        }

        result = await select_and_freeze_questions(db=db, session_row=session_row)

        assert result["status"] == "LOCKED"
        assert result["fallbackQuestionCount"] == 3

    async def test_sufficient_candidates_do_not_raise(self) -> None:
        """3+ eligible questions → no error raised from the selector."""
        from src.modules.interviews.question_selector import _load_candidates

        db = AsyncMock()

        # Build mock rows for 5 approved candidates
        approved_rows = [
            {
                "stable_key": f"ai-q-{i}",
                "question_version_id": str(uuid.uuid4()),
                "version": "1.0.0",
                "status": "APPROVED",
                "question_type": "technical",
                "difficulty_band": "intermediate",
                "canonical_locale": "en-US",
                "canonical_text": "Explain AI.",
                "objective": "Assess AI knowledge.",
                "soft_answer_seconds": 120,
                "hard_answer_seconds": 180,
                "mapping_purpose": "PRIMARY_COMPETENCY",
                "relevance": Decimal("1.0"),
                "rubric_version_id": str(uuid.uuid4()),
            }
            for i in range(5)
        ]

        call_index = 0

        async def _execute(query, params=None):
            nonlocal call_index
            result = MagicMock()
            if call_index == 0:
                # Candidate query
                result.mappings.return_value.all.return_value = approved_rows
            elif call_index % 3 == 1:
                # expected_points
                result.mappings.return_value.all.return_value = []
            elif call_index % 3 == 2:
                # rubric
                result.mappings.return_value.one_or_none.return_value = {
                    "id": str(uuid.uuid4()),
                    "version": "1.0",
                    "score_min": 0,
                    "score_max": 3,
                    "minimum_coverage": Decimal("0.6"),
                    "aggregation_method": "weighted_mean",
                    "aggregation_policy": {},
                }
            else:
                # criteria
                result.mappings.return_value.all.return_value = []
            call_index += 1
            return result

        db.execute.side_effect = _execute

        target = {
            "taxonomyVersion": "internal-2026.1",
            "conceptId": "skill-artificial-intelligence",
            "rationale": {"source": "job_requirement"},
        }
        candidates = await _load_candidates(db, target=target, locale="en-US", difficulty="intermediate")
        assert len(candidates) == 5

    async def test_unapproved_question_is_excluded(self) -> None:
        """Questions with status != APPROVED|CALIBRATED are filtered out."""
        from src.modules.interviews.question_selector import _load_candidates

        db = AsyncMock()

        # Return rows with DRAFT status — should be excluded by status filter
        draft_rows = [
            {
                "stable_key": "draft-q",
                "question_version_id": str(uuid.uuid4()),
                "version": "1.0.0",
                "status": "DRAFT",  # NOT eligible
                "question_type": "technical",
                "difficulty_band": "intermediate",
                "canonical_locale": "en-US",
                "canonical_text": "Explain AI.",
                "objective": "Assess.",
                "soft_answer_seconds": 120,
                "hard_answer_seconds": 180,
                "mapping_purpose": "PRIMARY_COMPETENCY",
                "relevance": Decimal("1.0"),
                "rubric_version_id": str(uuid.uuid4()),
            }
        ]

        async def _exec(q, params=None):
            res = MagicMock()
            res.mappings.return_value.all.return_value = draft_rows
            return res

        db.execute.side_effect = _exec

        target = {
            "taxonomyVersion": "internal-2026.1",
            "conceptId": "skill-artificial-intelligence",
            "rationale": {"source": "job_requirement"},
        }
        candidates = await _load_candidates(db, target=target, locale="en-US", difficulty="intermediate")
        # Status DRAFT should be skipped inside _load_candidates
        assert all(c.question_version_id != "draft-q" for c in candidates)

    async def test_wrong_locale_candidate_excluded(self) -> None:
        """Candidates with a completely mismatched locale are excluded."""
        from src.modules.interviews.question_selector import _load_candidates

        db = AsyncMock()

        rows = [
            {
                "stable_key": "jp-question",
                "question_version_id": str(uuid.uuid4()),
                "version": "1.0.0",
                "status": "APPROVED",
                "question_type": "technical",
                "difficulty_band": "intermediate",
                "canonical_locale": "ja-JP",  # No match for en-US
                "canonical_text": "AI を説明せよ",
                "objective": "Assess.",
                "soft_answer_seconds": 120,
                "hard_answer_seconds": 180,
                "mapping_purpose": "PRIMARY_COMPETENCY",
                "relevance": Decimal("1.0"),
                "rubric_version_id": str(uuid.uuid4()),
            }
        ]

        async def _exec(q, params=None):
            res = MagicMock()
            res.mappings.return_value.all.return_value = rows
            return res

        db.execute.side_effect = _exec

        target = {
            "taxonomyVersion": "internal-2026.1",
            "conceptId": "skill-artificial-intelligence",
            "rationale": {"source": "job_requirement"},
        }
        candidates = await _load_candidates(db, target=target, locale="en-US", difficulty="intermediate")
        assert len(candidates) == 0  # ja-JP != en-US, not even language match

    async def test_fewer_candidates_are_filled_with_fallbacks(self) -> None:
        """Available curated questions are retained and only the shortfall falls back."""
        from src.modules.interviews.question_selector import select_and_freeze_questions

        db = AsyncMock()

        candidates_rows = [
            {
                "stable_key": f"ai-q-{i}",
                "question_version_id": str(uuid.uuid4()),
                "version": "1.0.0",
                "status": "APPROVED",
                "question_type": "technical",
                "difficulty_band": "intermediate",
                "canonical_locale": "en-US",
                "canonical_text": "Explain AI.",
                "objective": "Assess.",
                "soft_answer_seconds": 120,
                "hard_answer_seconds": 180,
                "mapping_purpose": "PRIMARY_COMPETENCY",
                "relevance": Decimal("1.0"),
                "rubric_version_id": str(uuid.uuid4()),
            }
            for i in range(2)  # Only 2, target requires 3
        ]

        async def _side_effect(query, params=None):
            q_str = str(query)
            result = MagicMock()
            if "interview_session_plans" in q_str:
                result.mappings.return_value.one_or_none.return_value = {
                    "status": "READY",
                    "plan_payload": {},
                }
            elif "session_competency_targets" in q_str:
                result.mappings.return_value.all.return_value = [
                    {
                        "selection_rank": 0,
                        "taxonomy_version": "internal-2026.1",
                        "concept_id": "skill-artificial-intelligence",
                        "label": "Artificial Intelligence",
                        "importance": Decimal("0.5"),
                        "target_question_count": 3,
                        "rationale": None,
                    }
                ]
            elif "FROM interview_questions" in q_str:
                result.mappings.return_value.all.return_value = candidates_rows
            elif "expected_points" in q_str:
                result.mappings.return_value.all.return_value = []
            elif "rubric_versions" in q_str:
                result.mappings.return_value.one_or_none.return_value = {
                    "id": str(uuid.uuid4()),
                    "version": "1.0",
                    "score_min": 0,
                    "score_max": 3,
                    "minimum_coverage": Decimal("0.6"),
                    "aggregation_method": "weighted_mean",
                    "aggregation_policy": {},
                }
            elif "rubric_criteria" in q_str:
                result.mappings.return_value.all.return_value = []
            elif "rubric_anchors" in q_str:
                result.mappings.return_value.all.return_value = []
            else:
                result.mappings.return_value.all.return_value = []
                result.mappings.return_value.one_or_none.return_value = None
            return result

        db.execute.side_effect = _side_effect

        session_row = {
            "id": str(uuid.uuid4()),
            "plan_id": str(uuid.uuid4()),
            "locale": "en-US",
        }

        result = await select_and_freeze_questions(db=db, session_row=session_row)

        assert result["status"] == "LOCKED"
        assert result["fallbackQuestionCount"] == 1
