from src.modules.interviews.question_selector import (
    _allowed_purposes,
    _Candidate,
    _candidate_rank,
    _snapshot,
)


def candidate(**overrides):
    values = {
        "question_version_id": "qv-1",
        "rubric_version_id": "rv-1",
        "stable_key": "python-basics",
        "version": "1",
        "question_type": "technical",
        "difficulty_band": "foundational",
        "canonical_locale": "en-US",
        "question_text": "Explain a Python concept.",
        "objective": "Assess Python fundamentals.",
        "soft_answer_seconds": 60,
        "hard_answer_seconds": 120,
        "mapping_purpose": "TARGET_SKILL",
        "relevance": 0.9,
        "expected_points": [{"stable_key": "p1", "description": "Grounded point"}],
        "rubric": {"rubricVersionId": "rv-1", "criteria": []},
    }
    values.update(overrides)
    return _Candidate(**values)


def test_requirement_target_accepts_skill_and_primary_competency():
    target = {"rationale": {"source": "job_requirement"}}
    assert _allowed_purposes(target) == ("TARGET_SKILL", "PRIMARY_COMPETENCY")


def test_career_fallback_only_accepts_role_mapping():
    target = {"rationale": {"source": "career_classification_fallback"}}
    assert _allowed_purposes(target) == ("TARGET_ROLE",)


def test_rank_prefers_exact_locale_then_nearest_difficulty():
    exact = candidate(
        question_version_id="exact",
        canonical_locale="vi-VN",
        difficulty_band="intermediate",
    )
    language_only = candidate(
        question_version_id="lang",
        canonical_locale="vi",
        difficulty_band="intermediate",
    )
    harder = candidate(
        question_version_id="hard",
        canonical_locale="vi-VN",
        difficulty_band="advanced",
    )

    ranked = sorted(
        [language_only, harder, exact],
        key=lambda item: _candidate_rank(item, difficulty="intermediate", locale="vi-VN"),
    )
    assert [item.question_version_id for item in ranked] == ["exact", "hard", "lang"]


def test_snapshot_freezes_question_rubric_expected_points_and_target():
    item = candidate()
    target = {
        "taxonomyVersion": "tax-v1",
        "conceptId": "skill.python",
        "label": "Python",
    }
    snapshot = _snapshot(item, target=target, locale="en-US", selection_rank=2)

    assert snapshot["questionVersionId"] == "qv-1"
    assert snapshot["rubric"]["rubricVersionId"] == "rv-1"
    assert snapshot["expectedPoints"][0]["stable_key"] == "p1"
    assert snapshot["taxonomyTarget"]["conceptId"] == "skill.python"
    assert snapshot["taxonomyTarget"]["mappingPurpose"] == "TARGET_SKILL"
    assert snapshot["selectionRank"] == 2


def test_rank_salt_creates_session_level_diversity():
    c1 = candidate(question_version_id="qv-1111", stable_key="key-a")
    c2 = candidate(question_version_id="qv-2222", stable_key="key-b")

    # Without salt, key-a is always first
    ranked_default = sorted([c2, c1], key=lambda item: _candidate_rank(item, difficulty="foundational", locale="en-US"))
    assert ranked_default[0].question_version_id == "qv-1111"

    # With session salt, ordering is deterministic per session
    rank_a = sorted([c1, c2], key=lambda item: _candidate_rank(item, difficulty="foundational", locale="en-US", salt="session-alpha"))
    rank_b = sorted([c1, c2], key=lambda item: _candidate_rank(item, difficulty="foundational", locale="en-US", salt="session-alpha"))
    assert rank_a == rank_b


# ===========================================================================
# Gate 3 Dynamic Packing Tests (TC-PACK-01 .. TC-PACK-08)
# ===========================================================================

import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.modules.interviews.question_selector import (
    QuestionUnavailableError,
    _estimated_cost,
    _matches_archetype,
    _pack_target_questions,
    _find_feasible_subsets,
    _subset_objective,
    _solve_global_question_assignment,
    select_and_freeze_questions,
)


def test_tc_pack_01_floor_feasibility_preservation():
    """TC-PACK-01: Fixture 300 / 180 / 180.
    Envelope = 360, Floor = 360.
    Candidate A: cost = 300, rel = 0.95
    Candidate B: cost = 180, rel = 0.85
    Candidate C: cost = 180, rel = 0.80
    Expected: selected = {B, C} (total 360s). Must NOT select {A} (300s < floor 360s).
    """
    cand_a = candidate(
        question_version_id="qv-A",
        soft_answer_seconds=300,
        thinking_seconds=0,
        relevance=0.95,
        question_type="technical",
    )
    cand_b = candidate(
        question_version_id="qv-B",
        soft_answer_seconds=180,
        thinking_seconds=0,
        relevance=0.85,
        question_type="technical",
    )
    cand_c = candidate(
        question_version_id="qv-C",
        soft_answer_seconds=180,
        thinking_seconds=0,
        relevance=0.80,
        question_type="technical",
    )

    selected = _pack_target_questions(
        [cand_a, cand_b, cand_c],
        target_archetype="TEXT",
        floor_seconds=360,
        time_envelope_seconds=360,
        difficulty="intermediate",
        locale="en-US",
        session_id="test-session-pack-01",
    )

    assert selected is not None
    selected_ids = {q.question_version_id for q in selected}
    assert selected_ids == {"qv-B", "qv-C"}
    total_cost = sum(_estimated_cost(q) for q in selected)
    assert total_cost == 360
    assert total_cost >= 360  # Floor satisfied
    assert total_cost <= 360  # Hard ceiling satisfied


@pytest.mark.asyncio
async def test_tc_pack_02_insufficient_floor_fail_closed():
    """TC-PACK-02: Q2 Fail-Closed on Insufficient Floor.
    Envelope = 360, Floor = 360. Only Candidate A = 300s.
    Expected:
    - Pure packing returns None.
    - select_and_freeze_questions raises QuestionUnavailableError with code question_bank_insufficient.
    - 0 turns written, plan status unchanged.
    """
    cand_a = candidate(
        question_version_id="qv-A",
        soft_answer_seconds=300,
        thinking_seconds=0,
        relevance=0.95,
        question_type="technical",
    )

    # 1. Pure function assertion
    res = _pack_target_questions(
        [cand_a],
        target_archetype="TEXT",
        floor_seconds=360,
        time_envelope_seconds=360,
        difficulty="intermediate",
        locale="en-US",
        session_id="test-session-pack-02",
    )
    assert res is None

    # 2. End-to-end P2 dispatch assertion
    db = AsyncMock()
    plan_result = MagicMock()
    plan_result.mappings.return_value.one_or_none.return_value = {
        "status": "READY",
        "plan_payload": {
            "policyVersion": "interview-planner-v2-dynamic",
            "targets": [
                {
                    "conceptId": "concept-kafka",
                    "label": "Apache Kafka",
                    "targetArchetype": "TEXT",
                    "floorSeconds": 360,
                    "timeEnvelopeSeconds": 360,
                    "taxonomyVersion": "internal-2026.1",
                }
            ],
        },
    }

    candidates_result = MagicMock()
    candidates_result.mappings.return_value.all.return_value = [
        {
            "stable_key": "k-a",
            "question_version_id": "qv-A",
            "version": "1.0",
            "status": "APPROVED",
            "question_type": "technical",
            "difficulty_band": "intermediate",
            "canonical_locale": "en-US",
            "canonical_text": "Kafka question",
            "objective": "Assess Kafka",
            "thinking_seconds": 0,
            "soft_answer_seconds": 300,
            "hard_answer_seconds": 360,
            "mapping_purpose": "TARGET_SKILL",
            "relevance": 0.95,
            "rubric_version_id": "rv-1",
        }
    ]

    rubric_result = MagicMock()
    rubric_result.mappings.return_value.one_or_none.return_value = {
        "id": "rv-1",
        "version": "1.0",
        "score_min": 0,
        "score_max": 3,
        "minimum_coverage": 0.6,
        "aggregation_method": "weighted_mean",
        "aggregation_policy": {},
    }

    async def _execute_mock(query, params=None):
        q_str = str(query)
        if "interview_session_plans" in q_str:
            return plan_result
        if "FROM interview_questions" in q_str:
            return candidates_result
        if "rubric_versions" in q_str:
            return rubric_result
        m = MagicMock()
        m.mappings.return_value.all.return_value = []
        m.mappings.return_value.one_or_none.return_value = None
        return m

    db.execute.side_effect = _execute_mock

    session_row = {"id": "sess-tc-02", "plan_id": "plan-tc-02", "locale": "en-US"}

    with pytest.raises(QuestionUnavailableError) as exc_info:
        await select_and_freeze_questions(db=db, session_row=session_row)

    err = exc_info.value
    assert err.error_code == "question_bank_insufficient"
    payload = err.to_payload()
    assert payload["errorCode"] == "question_bank_insufficient"
    assert "missingTargets" in payload["details"]
    missing = payload["details"]["missingTargets"]
    assert len(missing) == 1
    assert missing[0]["conceptId"] == "concept-kafka"
    assert missing[0]["reason"] == "no_feasible_qualifying_subset"

    # Invariant check: DELETE/INSERT into interview_turns was NEVER executed
    for call_args in db.execute.call_args_list:
        called_sql = str(call_args[0][0])
        assert "DELETE FROM interview_turns" not in called_sql
        assert "INSERT INTO interview_turns" not in called_sql
        assert "UPDATE interview_session_plans" not in called_sql


def test_tc_pack_03_archetype_strictness_fail_closed():
    """TC-PACK-03: TEXT target only has CODING candidates.
    Expected:
    - Pure packing returns None.
    - No coding fallback allowed for TEXT target.
    """
    coding_cand = candidate(
        question_version_id="qv-coding-1",
        question_type="coding",
        soft_answer_seconds=240,
        thinking_seconds=60,
    )

    # TEXT target rejects CODING question
    res = _pack_target_questions(
        [coding_cand],
        target_archetype="TEXT",
        floor_seconds=180,
        time_envelope_seconds=360,
        difficulty="intermediate",
        locale="en-US",
        session_id="sess-tc-03",
    )
    assert res is None


def test_tc_pack_04_hard_ceiling_budget_non_exceedance():
    """TC-PACK-04: Envelope = 300, Floor = 180.
    Candidates: 350s, 400s (both exceed envelope 300s).
    Expected:
    - Both candidates pruned immediately.
    - Returns None.
    """
    cand1 = candidate(
        question_version_id="qv-350",
        soft_answer_seconds=350,
        thinking_seconds=0,
    )
    cand2 = candidate(
        question_version_id="qv-400",
        soft_answer_seconds=400,
        thinking_seconds=0,
    )

    res = _pack_target_questions(
        [cand1, cand2],
        target_archetype="TEXT",
        floor_seconds=180,
        time_envelope_seconds=300,
        difficulty="intermediate",
        locale="en-US",
        session_id="sess-tc-04",
    )
    assert res is None


@pytest.mark.asyncio
async def test_tc_pack_05_atomic_preflight_no_partial_queue():
    """TC-PACK-05: 2 targets. Target 1 feasible, Target 2 impossible.
    Expected:
    - Fail-closed exception raised.
    - 0 partial turns written in DB for Target 1.
    - Plan not locked.
    """
    db = AsyncMock()
    plan_result = MagicMock()
    plan_result.mappings.return_value.one_or_none.return_value = {
        "status": "READY",
        "plan_payload": {
            "policyVersion": "interview-planner-v2-dynamic",
            "targets": [
                {
                    "conceptId": "concept-1-feasible",
                    "label": "Concept 1",
                    "targetArchetype": "TEXT",
                    "floorSeconds": 180,
                    "timeEnvelopeSeconds": 240,
                    "taxonomyVersion": "internal-2026.1",
                },
                {
                    "conceptId": "concept-2-impossible",
                    "label": "Concept 2",
                    "targetArchetype": "TEXT",
                    "floorSeconds": 360,
                    "timeEnvelopeSeconds": 360,
                    "taxonomyVersion": "internal-2026.1",
                },
            ],
        },
    }

    t1_candidate_row = {
        "stable_key": "k-1",
        "question_version_id": "qv-target1",
        "version": "1.0",
        "status": "APPROVED",
        "question_type": "technical",
        "difficulty_band": "intermediate",
        "canonical_locale": "en-US",
        "canonical_text": "Target 1 question",
        "objective": "Assess",
        "thinking_seconds": 0,
        "soft_answer_seconds": 180,
        "hard_answer_seconds": 240,
        "mapping_purpose": "TARGET_SKILL",
        "relevance": 0.9,
        "rubric_version_id": "rv-1",
    }

    async def _execute_mock(query, params=None):
        q_str = str(query)
        if "interview_session_plans" in q_str:
            return plan_result
        if "FROM interview_questions" in q_str:
            res = MagicMock()
            if params and params.get("concept_id") == "concept-1-feasible":
                res.mappings.return_value.all.return_value = [t1_candidate_row]
            else:
                res.mappings.return_value.all.return_value = []  # Target 2 has 0 candidates
            return res
        if "rubric_versions" in q_str:
            res = MagicMock()
            res.mappings.return_value.one_or_none.return_value = {
                "id": "rv-1",
                "version": "1.0",
                "score_min": 0,
                "score_max": 3,
                "minimum_coverage": 0.6,
                "aggregation_method": "weighted_mean",
                "aggregation_policy": {},
            }
            return res
        m = MagicMock()
        m.mappings.return_value.all.return_value = []
        m.mappings.return_value.one_or_none.return_value = None
        return m

    db.execute.side_effect = _execute_mock

    session_row = {"id": "sess-tc-05", "plan_id": "plan-tc-05", "locale": "en-US"}

    with pytest.raises(QuestionUnavailableError) as exc_info:
        await select_and_freeze_questions(db=db, session_row=session_row)

    assert exc_info.value.error_code == "question_bank_insufficient"
    missing = exc_info.value.details["missingTargets"]
    assert len(missing) == 1
    assert missing[0]["conceptId"] == "concept-2-impossible"

    # Confirm 0 turns written
    for call_args in db.execute.call_args_list:
        called_sql = str(call_args[0][0])
        assert "DELETE FROM interview_turns" not in called_sql
        assert "INSERT INTO interview_turns" not in called_sql
        assert "UPDATE interview_session_plans" not in called_sql


def test_tc_pack_06_deterministic_selection_vs_sql_shuffle():
    """TC-PACK-06: Same candidate pool, different input orders (reversed/shuffled).
    Expected:
    - Same selected subset.
    - Same intra-subset turn order.
    - Same assigned order.
    """
    c1 = candidate(
        question_version_id="qv-01",
        stable_key="key-01",
        soft_answer_seconds=180,
        relevance=0.90,
        difficulty_band="intermediate",
    )
    c2 = candidate(
        question_version_id="qv-02",
        stable_key="key-02",
        soft_answer_seconds=180,
        relevance=0.85,
        difficulty_band="intermediate",
    )
    c3 = candidate(
        question_version_id="qv-03",
        stable_key="key-03",
        soft_answer_seconds=180,
        relevance=0.80,
        difficulty_band="intermediate",
    )

    kwargs = {
        "target_archetype": "TEXT",
        "floor_seconds": 360,
        "time_envelope_seconds": 360,
        "difficulty": "intermediate",
        "locale": "en-US",
        "session_id": "sess-shuffle-test",
    }

    res_forward = _pack_target_questions([c1, c2, c3], **kwargs)
    res_reverse = _pack_target_questions([c3, c2, c1], **kwargs)
    res_permuted = _pack_target_questions([c2, c3, c1], **kwargs)

    assert res_forward is not None
    assert res_reverse is not None
    assert res_permuted is not None

    ids_forward = [q.question_version_id for q in res_forward]
    ids_reverse = [q.question_version_id for q in res_reverse]
    ids_permuted = [q.question_version_id for q in res_permuted]

    assert ids_forward == ids_reverse == ids_permuted
    assert ids_forward == ["qv-01", "qv-02"]


def test_tc_pack_07_single_question_validity():
    """TC-PACK-07: Envelope = 200, Floor = 180.
    Single candidate with cost = 190.
    Expected:
    - Exactly 1 question selected.
    - Does NOT force >= 2 questions.
    """
    cand = candidate(
        question_version_id="qv-single-190",
        soft_answer_seconds=190,
        thinking_seconds=0,
        question_type="technical",
    )

    selected = _pack_target_questions(
        [cand],
        target_archetype="TEXT",
        floor_seconds=180,
        time_envelope_seconds=200,
        difficulty="intermediate",
        locale="en-US",
        session_id="sess-tc-07",
    )

    assert selected is not None
    assert len(selected) == 1
    assert selected[0].question_version_id == "qv-single-190"


@pytest.mark.asyncio
async def test_tc_pack_08_legacy_branch_invariance(monkeypatch):
    """TC-PACK-08: Plan with policyVersion = 'interview-planner-v1'.
    Candidate pool lacks questions (only 1 available, needed = 2).
    Expected:
    - Legacy branch executes.
    - Fail closed with question_bank_insufficient.
    - _fallback_snapshot is not used and no partial writes are issued.
    """
    db = AsyncMock()
    plan_result = MagicMock()
    plan_result.mappings.return_value.one_or_none.return_value = {
        "status": "READY",
        "plan_payload": {
            "policyVersion": "interview-planner-v1",
            "difficulty": {"level": "intermediate"},
        },
    }

    targets_result = MagicMock()
    targets_result.mappings.return_value.all.return_value = [
        {
            "selection_rank": 0,
            "taxonomy_version": "internal-2026.1",
            "concept_id": "legacy-concept",
            "label": "Legacy Concept",
            "importance": 1.0,
            "target_question_count": 2,
            "rationale": None,
        }
    ]

    candidates_result = MagicMock()
    candidates_result.mappings.return_value.all.return_value = [
        {
            "stable_key": "leg-q1",
            "question_version_id": "qv-leg-1",
            "version": "1.0",
            "status": "APPROVED",
            "question_type": "technical",
            "difficulty_band": "intermediate",
            "canonical_locale": "en-US",
            "canonical_text": "Legacy Q1",
            "objective": "Assess",
            "thinking_seconds": 0,
            "soft_answer_seconds": 180,
            "hard_answer_seconds": 240,
            "mapping_purpose": "PRIMARY_COMPETENCY",
            "relevance": 1.0,
            "rubric_version_id": "rv-leg-1",
        }
    ]

    rubric_result = MagicMock()
    rubric_result.mappings.return_value.one_or_none.return_value = {
        "id": "rv-leg-1",
        "version": "1.0",
        "score_min": 0,
        "score_max": 3,
        "minimum_coverage": 0.6,
        "aggregation_method": "weighted_mean",
        "aggregation_policy": {},
    }

    turns_result = MagicMock()
    turns_result.mappings.return_value.all.return_value = [
        {
            "id": "turn-1",
            "turn_index": 0,
            "status": "PLANNED",
            "question_version_id": None,
            "rubric_version_id": None,
            "question_snapshot": {},
        }
    ]

    async def _execute_mock(query, params=None):
        q_str = str(query)
        if "interview_session_plans" in q_str:
            return plan_result
        if "session_competency_targets" in q_str:
            return targets_result
        if "FROM interview_questions" in q_str:
            return candidates_result
        if "rubric_versions" in q_str:
            return rubric_result
        if "SELECT id, turn_index" in q_str:
            return turns_result
        m = MagicMock()
        m.mappings.return_value.all.return_value = []
        m.mappings.return_value.one_or_none.return_value = None
        return m

    db.execute.side_effect = _execute_mock
    db.scalar.return_value = None

    session_row = {"id": "sess-tc-08", "plan_id": "plan-tc-08", "locale": "en-US"}

    from src.modules.interviews import question_selector

    fallback_spy = MagicMock(wraps=question_selector._fallback_snapshot)
    monkeypatch.setattr(question_selector, "_fallback_snapshot", fallback_spy)
    with pytest.raises(QuestionUnavailableError) as exc_info:
        await select_and_freeze_questions(db=db, session_row=session_row)

    err = exc_info.value
    assert err.error_code == "question_bank_insufficient"
    assert err.details == {
        "missingTargets": [
            {
                "taxonomyVersion": "internal-2026.1",
                "conceptId": "legacy-concept",
                "needed": 2,
                "available": 1,
            }
        ]
    }
    payload = err.to_payload()
    serialized = json.dumps(payload)
    assert "Legacy Q1" not in serialized
    assert "Assess" not in serialized
    assert "rv-leg-1" not in serialized
    fallback_spy.assert_not_called()

    for call_args in db.execute.call_args_list:
        called_sql = str(call_args[0][0])
        assert "DELETE FROM interview_turns" not in called_sql
        assert "INSERT INTO interview_turns" not in called_sql
        assert "UPDATE interview_session_plans" not in called_sql


@pytest.mark.asyncio
async def test_legacy_plan_with_enough_candidates_freezes_without_fallback(monkeypatch):
    from src.modules.interviews import question_selector

    plan_result = MagicMock()
    plan_result.mappings.return_value.one_or_none.return_value = {
        "status": "READY",
        "plan_payload": {"policyVersion": "interview-planner-v1", "difficulty": {"level": "intermediate"}},
    }
    targets_result = MagicMock()
    targets_result.mappings.return_value.all.return_value = [
        {
            "selection_rank": 0,
            "taxonomy_version": "internal-2026.1",
            "concept_id": "legacy-enough",
            "label": "Legacy Concept",
            "importance": 1.0,
            "target_question_count": 2,
            "rationale": None,
        }
    ]
    turns_result = MagicMock()
    turns_result.mappings.return_value.all.return_value = []

    async def execute(query, params=None):
        sql = str(query)
        if "SELECT status, plan_payload" in sql:
            return plan_result
        if "FROM session_competency_targets" in sql:
            return targets_result
        if "SELECT id, turn_index" in sql:
            return turns_result
        result = MagicMock()
        result.mappings.return_value.all.return_value = []
        return result

    async def load_candidates(db, *, target, **kwargs):
        return [
            candidate(question_version_id="legacy-q1", soft_answer_seconds=60),
            candidate(question_version_id="legacy-q2", soft_answer_seconds=60),
        ]

    db = AsyncMock()
    db.execute.side_effect = execute
    monkeypatch.setattr(question_selector, "_load_candidates", load_candidates)
    fallback_spy = MagicMock(wraps=question_selector._fallback_snapshot)
    monkeypatch.setattr(question_selector, "_fallback_snapshot", fallback_spy)

    result = await select_and_freeze_questions(
        db=db,
        session_row={"id": "legacy-enough-session", "plan_id": "legacy-plan", "locale": "en-US"},
    )

    assert result["status"] == "LOCKED"
    assert result["fallbackQuestionCount"] == 0
    assert any("INSERT INTO interview_turns" in str(call[0][0]) for call in db.execute.call_args_list)
    fallback_spy.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_missing_later_target_writes_no_partial_turns(monkeypatch):
    from src.modules.interviews import question_selector

    plan_result = MagicMock()
    plan_result.mappings.return_value.one_or_none.return_value = {
        "status": "READY",
        "plan_payload": {"policyVersion": "interview-planner-v1", "difficulty": {"level": "intermediate"}},
    }
    targets_result = MagicMock()
    targets_result.mappings.return_value.all.return_value = [
        {
            "selection_rank": rank,
            "taxonomy_version": "internal-2026.1",
            "concept_id": concept,
            "label": concept,
            "importance": 1.0,
            "target_question_count": 2,
            "rationale": None,
        }
        for rank, concept in enumerate(("first-target", "later-target"))
    ]

    async def execute(query, params=None):
        if "SELECT status, plan_payload" in str(query):
            return plan_result
        if "FROM session_competency_targets" in str(query):
            return targets_result
        result = MagicMock()
        result.mappings.return_value.all.return_value = []
        return result

    async def load_candidates(db, *, target, **kwargs):
        if target["conceptId"] == "later-target":
            return []
        return [
            candidate(question_version_id="first-q1", soft_answer_seconds=60),
            candidate(question_version_id="first-q2", soft_answer_seconds=60),
        ]

    db = AsyncMock()
    db.execute.side_effect = execute
    monkeypatch.setattr(question_selector, "_load_candidates", load_candidates)
    with pytest.raises(QuestionUnavailableError) as exc_info:
        await select_and_freeze_questions(
            db=db,
            session_row={"id": "legacy-multi-target", "plan_id": "legacy-plan", "locale": "en-US"},
        )

    assert exc_info.value.details["missingTargets"][0]["conceptId"] == "later-target"
    for call in db.execute.call_args_list:
        sql = str(call[0][0])
        assert "DELETE FROM interview_turns" not in sql
        assert "INSERT INTO interview_turns" not in sql
        assert "UPDATE interview_session_plans" not in sql


@pytest.mark.asyncio
async def test_locked_legacy_fallback_turn_is_returned_unchanged():
    old_snapshot = {
        "questionSource": "deterministic_fallback_unreviewed",
        "questionText": "Historical frozen text",
        "rubric": None,
        "stableKey": "legacy-fallback",
    }
    plan_result = MagicMock()
    plan_result.mappings.return_value.one_or_none.return_value = {
        "status": "LOCKED",
        "plan_payload": {"policyVersion": "interview-planner-v1"},
    }
    turns_result = MagicMock()
    turns_result.mappings.return_value.all.return_value = [
        {
            "id": "old-turn",
            "turn_index": 2,
            "status": "PLANNED",
            "question_version_id": None,
            "rubric_version_id": None,
            "question_snapshot": old_snapshot,
        }
    ]
    db = AsyncMock()

    async def execute(query, params=None):
        if "interview_session_plans" in str(query):
            return plan_result
        return turns_result

    db.execute.side_effect = execute
    result = await select_and_freeze_questions(
        db=db,
        session_row={"id": "old-session", "plan_id": "old-plan", "locale": "en-US"},
    )

    assert result["turns"][0]["question"] == old_snapshot
    assert result["fallbackQuestionCount"] == 1
    assert not any("INSERT INTO interview_turns" in str(c[0][0]) for c in db.execute.call_args_list)
    assert not any("DELETE FROM interview_turns" in str(c[0][0]) for c in db.execute.call_args_list)


@pytest.mark.asyncio
async def test_question_selection_api_maps_insufficiency_to_safe_409(monkeypatch):
    import importlib
    from fastapi import HTTPException

    router_module = importlib.import_module("src.modules.interviews.router")

    async def owned_session(*args, **kwargs):
        return {"id": "safe-session"}

    async def insufficient(*args, **kwargs):
        raise QuestionUnavailableError(
            "Question bank cannot satisfy interview plan requirements",
            details={
                "missingTargets": [
                    {
                        "taxonomyVersion": "internal-2026.1",
                        "conceptId": "safe-concept",
                        "needed": 2,
                        "available": 1,
                    }
                ]
            },
        )

    monkeypatch.setattr(router_module, "_owned_session", owned_session)
    monkeypatch.setattr(router_module, "select_and_freeze_questions", insufficient)

    with pytest.raises(HTTPException) as exc_info:
        await router_module.select_interview_questions(
            session_id="safe-session",
            user={"sub": "user"},
            db=AsyncMock(),
        )

    assert exc_info.value.status_code == 409
    detail = exc_info.value.detail
    assert detail["errorCode"] == "question_bank_insufficient"
    assert detail["details"]["missingTargets"][0]["conceptId"] == "safe-concept"
    serialized = json.dumps(detail)
    assert "questionText" not in serialized
    assert "rubric" not in serialized


@pytest.mark.asyncio
async def test_tc_pack_09_cross_target_allocation_correctness():
    """TC-PACK-09: Cross-Target Allocation Correctness.

    Fixture:
    Target T1:
      - Candidate X (relevance 0.95, cost 180s)
      - Candidate Y (relevance 0.80, cost 180s)
    Target T2:
      - Candidate X only (cost 180s)

    Global Feasible Assignment exists:
      T1 -> Y
      T2 -> X

    Expected:
      Policy 2 Global Solver must find the conflict-free assignment:
      T1 gets {Y}, T2 gets {X}.
      No 409 error.
      No duplicate question_version_id.
      All targets satisfy floor and envelope.
    """
    db = AsyncMock()
    plan_result = MagicMock()
    plan_result.mappings.return_value.one_or_none.return_value = {
        "status": "READY",
        "plan_payload": {
            "policyVersion": "interview-planner-v2-dynamic",
            "targets": [
                {
                    "conceptId": "target-t1",
                    "label": "Target 1",
                    "targetArchetype": "TEXT",
                    "floorSeconds": 180,
                    "timeEnvelopeSeconds": 360,
                    "taxonomyVersion": "internal-2026.1",
                    "selectionRank": 1,
                    "importance": 1.0,
                },
                {
                    "conceptId": "target-t2",
                    "label": "Target 2",
                    "targetArchetype": "TEXT",
                    "floorSeconds": 180,
                    "timeEnvelopeSeconds": 360,
                    "taxonomyVersion": "internal-2026.1",
                    "selectionRank": 2,
                    "importance": 1.0,
                },
            ],
        },
    }

    cand_x_row = {
        "stable_key": "k-x",
        "question_version_id": "qv-x",
        "version": "1.0",
        "status": "APPROVED",
        "question_type": "technical",
        "difficulty_band": "intermediate",
        "canonical_locale": "en-US",
        "canonical_text": "Question X",
        "objective": "Assess X",
        "thinking_seconds": 0,
        "soft_answer_seconds": 180,
        "hard_answer_seconds": 240,
        "mapping_purpose": "PRIMARY_COMPETENCY",
        "relevance": 0.95,
        "rubric_version_id": "rv-1",
    }
    cand_y_row = {
        "stable_key": "k-y",
        "question_version_id": "qv-y",
        "version": "1.0",
        "status": "APPROVED",
        "question_type": "technical",
        "difficulty_band": "intermediate",
        "canonical_locale": "en-US",
        "canonical_text": "Question Y",
        "objective": "Assess Y",
        "thinking_seconds": 0,
        "soft_answer_seconds": 180,
        "hard_answer_seconds": 240,
        "mapping_purpose": "PRIMARY_COMPETENCY",
        "relevance": 0.80,
        "rubric_version_id": "rv-1",
    }

    inserted_turns = []

    async def _execute_mock(query, params=None):
        q_str = str(query)
        if "SELECT status, plan_payload" in q_str:
            return plan_result
        if "FROM interview_questions" in q_str:
            res = MagicMock()
            if params and params.get("concept_id") == "target-t1":
                # T1 has both X and Y
                res.mappings.return_value.all.return_value = [cand_x_row, cand_y_row]
            elif params and params.get("concept_id") == "target-t2":
                # T2 only has X
                res.mappings.return_value.all.return_value = [cand_x_row]
            else:
                res.mappings.return_value.all.return_value = []
            return res
        if "rubric_versions" in q_str:
            res = MagicMock()
            res.mappings.return_value.one_or_none.return_value = {
                "id": "rv-1",
                "version": "1.0",
                "score_min": 0,
                "score_max": 3,
                "minimum_coverage": 0.6,
                "aggregation_method": "weighted_mean",
                "aggregation_policy": {},
            }
            return res
        if "INSERT INTO interview_turns" in q_str:
            t_idx = params.get("turn_index")
            if t_idx is None:
                if ", 0," in q_str:
                    t_idx = 0
                elif ", 1," in q_str:
                    t_idx = 1
            inserted_turns.append(dict(params, turn_index=t_idx))
            return MagicMock()
        if "SELECT id, turn_index" in q_str:
            res = MagicMock()
            res.mappings.return_value.all.return_value = [
                {
                    "id": f"t-{p['turn_index']}",
                    "turn_index": p["turn_index"],
                    "status": "PLANNED",
                    "question_version_id": p.get("question_version_id"),
                    "rubric_version_id": p.get("rubric_version_id"),
                    "question_snapshot": json.loads(p["snapshot"]),
                }
                for p in inserted_turns
            ]
            return res
        m = MagicMock()
        m.mappings.return_value.all.return_value = []
        m.mappings.return_value.one_or_none.return_value = None
        return m

    db.execute.side_effect = _execute_mock

    session_row = {"id": "sess-tc-09", "plan_id": "plan-tc-09", "locale": "en-US"}
    result = await select_and_freeze_questions(db=db, session_row=session_row)

    assert result["status"] == "LOCKED"
    turns = result["turns"]
    # Turn 0: WARM_UP, Turn 1: VALIDATE, Turn 2: T1 question, Turn 3: T2 question, Turn 4: BEHAVIORAL
    assert len(turns) == 5
    technical_turns = [t for t in turns if t["question"].get("questionType") == "technical"]
    assert len(technical_turns) == 2

    # Verify T1 got Y and T2 got X
    t1_turn = next(t for t in technical_turns if t["turnIndex"] == 2)
    t2_turn = next(t for t in technical_turns if t["turnIndex"] == 3)
    assert t1_turn["questionVersionId"] == "qv-y"
    assert t2_turn["questionVersionId"] == "qv-x"

    # Assert no duplicate question_version_id
    qids = [t["questionVersionId"] for t in technical_turns]
    assert len(qids) == len(set(qids))


@pytest.mark.asyncio
async def test_tc_pack_10_unsatisfiable_global_assignment():
    """TC-PACK-10: Unsatisfiable Global Assignment (Fail-Closed).

    Fixture:
    Target T1: {Candidate X only}
    Target T2: {Candidate X only}
    Question reuse prohibited.

    Expected:
      QuestionUnavailableError with error_code="question_bank_insufficient".
      0 partial turns written.
      Plan remains unchanged.
    """
    db = AsyncMock()
    plan_result = MagicMock()
    plan_result.mappings.return_value.one_or_none.return_value = {
        "status": "READY",
        "plan_payload": {
            "policyVersion": "interview-planner-v2-dynamic",
            "targets": [
                {
                    "conceptId": "target-t1",
                    "label": "Target 1",
                    "targetArchetype": "TEXT",
                    "floorSeconds": 180,
                    "timeEnvelopeSeconds": 360,
                    "taxonomyVersion": "internal-2026.1",
                    "selectionRank": 1,
                    "importance": 1.0,
                },
                {
                    "conceptId": "target-t2",
                    "label": "Target 2",
                    "targetArchetype": "TEXT",
                    "floorSeconds": 180,
                    "timeEnvelopeSeconds": 360,
                    "taxonomyVersion": "internal-2026.1",
                    "selectionRank": 2,
                    "importance": 1.0,
                },
            ],
        },
    }

    cand_x_row = {
        "stable_key": "k-x",
        "question_version_id": "qv-x",
        "version": "1.0",
        "status": "APPROVED",
        "question_type": "technical",
        "difficulty_band": "intermediate",
        "canonical_locale": "en-US",
        "canonical_text": "Question X",
        "objective": "Assess X",
        "thinking_seconds": 0,
        "soft_answer_seconds": 180,
        "hard_answer_seconds": 240,
        "mapping_purpose": "PRIMARY_COMPETENCY",
        "relevance": 0.95,
        "rubric_version_id": "rv-1",
    }

    async def _execute_mock(query, params=None):
        q_str = str(query)
        if "SELECT status, plan_payload" in q_str:
            return plan_result
        if "FROM interview_questions" in q_str:
            res = MagicMock()
            res.mappings.return_value.all.return_value = [cand_x_row]
            return res
        if "rubric_versions" in q_str:
            res = MagicMock()
            res.mappings.return_value.one_or_none.return_value = {
                "id": "rv-1",
                "version": "1.0",
                "score_min": 0,
                "score_max": 3,
                "minimum_coverage": 0.6,
                "aggregation_method": "weighted_mean",
                "aggregation_policy": {},
            }
            return res
        m = MagicMock()
        m.mappings.return_value.all.return_value = []
        m.mappings.return_value.one_or_none.return_value = None
        return m

    db.execute.side_effect = _execute_mock

    session_row = {"id": "sess-tc-10", "plan_id": "plan-tc-10", "locale": "en-US"}

    with pytest.raises(QuestionUnavailableError) as exc_info:
        await select_and_freeze_questions(db=db, session_row=session_row)

    assert exc_info.value.error_code == "question_bank_insufficient"
    assert exc_info.value.details.get("reason") == "no_conflict_free_global_assignment"

    # Confirm 0 turns written and plan not locked
    for call_args in db.execute.call_args_list:
        called_sql = str(call_args[0][0])
        assert "DELETE FROM interview_turns" not in called_sql
        assert "INSERT INTO interview_turns" not in called_sql
        assert "UPDATE interview_session_plans" not in called_sql


@pytest.mark.asyncio
async def test_tc_pack_11_search_order_independence():
    """TC-PACK-11: Search Order Independence (Pure Solver + End-to-End Integration).

    Even if targets, candidates, or feasible subset enumerations are reversed / shuffled,
    the solver and end-to-end freeze pipeline must produce:
    - Identical final assignment
    - Identical frozen question IDs
    - Identical pedagogical turn ordering
    - Identical selection_contract & fingerprint
    """
    t1 = {
        "conceptId": "concept-1",
        "taxonomyVersion": "internal-2026.1",
        "selectionRank": 1,
        "importance": 1.0,
        "targetArchetype": "TEXT",
        "floorSeconds": 180,
        "timeEnvelopeSeconds": 360,
    }
    t2 = {
        "conceptId": "concept-2",
        "taxonomyVersion": "internal-2026.1",
        "selectionRank": 2,
        "importance": 1.0,
        "targetArchetype": "TEXT",
        "floorSeconds": 180,
        "timeEnvelopeSeconds": 360,
    }

    cand_x = candidate(question_version_id="qv-x", soft_answer_seconds=180, relevance=0.95)
    cand_y = candidate(question_version_id="qv-y", soft_answer_seconds=180, relevance=0.80)

    # 1. Pure solver verification
    domains_1 = {
        "internal-2026.1:concept-1": [
            (_subset_objective([cand_x], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id="salt-11"), [cand_x]),
            (_subset_objective([cand_y], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id="salt-11"), [cand_y]),
        ],
        "internal-2026.1:concept-2": [
            (_subset_objective([cand_x], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id="salt-11"), [cand_x]),
        ],
    }
    res1 = _solve_global_question_assignment([t1, t2], domains_1, session_id="salt-11")

    domains_2 = {
        "internal-2026.1:concept-1": [
            (_subset_objective([cand_y], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id="salt-11"), [cand_y]),
            (_subset_objective([cand_x], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id="salt-11"), [cand_x]),
        ],
        "internal-2026.1:concept-2": [
            (_subset_objective([cand_x], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id="salt-11"), [cand_x]),
        ],
    }
    res2 = _solve_global_question_assignment([t2, t1], domains_2, session_id="salt-11")

    assert res1 is not None and res2 is not None
    assert [q.question_version_id for q in res1["internal-2026.1:concept-1"]] == ["qv-y"]
    assert [q.question_version_id for q in res1["internal-2026.1:concept-2"]] == ["qv-x"]
    assert [q.question_version_id for q in res2["internal-2026.1:concept-1"]] == ["qv-y"]
    assert [q.question_version_id for q in res2["internal-2026.1:concept-2"]] == ["qv-x"]

    # 2. Integration-level verification of select_and_freeze_questions
    cand_x_row = {
        "stable_key": "k-x",
        "question_version_id": "qv-x",
        "version": "1.0",
        "status": "APPROVED",
        "question_type": "technical",
        "difficulty_band": "intermediate",
        "canonical_locale": "en-US",
        "canonical_text": "Question X",
        "objective": "Assess X",
        "thinking_seconds": 0,
        "soft_answer_seconds": 180,
        "hard_answer_seconds": 240,
        "mapping_purpose": "PRIMARY_COMPETENCY",
        "relevance": 0.95,
        "rubric_version_id": "rv-1",
    }
    cand_y_row = {
        "stable_key": "k-y",
        "question_version_id": "qv-y",
        "version": "1.0",
        "status": "APPROVED",
        "question_type": "technical",
        "difficulty_band": "intermediate",
        "canonical_locale": "en-US",
        "canonical_text": "Question Y",
        "objective": "Assess Y",
        "thinking_seconds": 0,
        "soft_answer_seconds": 180,
        "hard_answer_seconds": 240,
        "mapping_purpose": "PRIMARY_COMPETENCY",
        "relevance": 0.80,
        "rubric_version_id": "rv-1",
    }

    async def _run_freeze_pipeline(target_list, concept1_candidates):
        db = AsyncMock()
        plan_result = MagicMock()
        plan_result.mappings.return_value.one_or_none.return_value = {
            "status": "READY",
            "plan_payload": {
                "policyVersion": "interview-planner-v2-dynamic",
                "targets": target_list,
            },
        }
        inserted_turns = []
        updated_payload = {}

        async def _exec(query, params=None):
            q_str = str(query)
            if "SELECT status, plan_payload" in q_str:
                return plan_result
            if "FROM interview_questions" in q_str:
                res = MagicMock()
                if params and params.get("concept_id") == "concept-1":
                    res.mappings.return_value.all.return_value = concept1_candidates
                elif params and params.get("concept_id") == "concept-2":
                    res.mappings.return_value.all.return_value = [cand_x_row]
                else:
                    res.mappings.return_value.all.return_value = []
                return res
            if "rubric_versions" in q_str:
                res = MagicMock()
                res.mappings.return_value.one_or_none.return_value = {
                    "id": "rv-1",
                    "version": "1.0",
                    "score_min": 0,
                    "score_max": 3,
                    "minimum_coverage": 0.6,
                    "aggregation_method": "weighted_mean",
                    "aggregation_policy": {},
                }
                return res
            if "INSERT INTO interview_turns" in q_str:
                t_idx = params.get("turn_index")
                if t_idx is None:
                    if ", 0," in q_str:
                        t_idx = 0
                    elif ", 1," in q_str:
                        t_idx = 1
                inserted_turns.append(dict(params, turn_index=t_idx))
                return MagicMock()
            if "UPDATE interview_session_plans" in q_str:
                nonlocal updated_payload
                updated_payload = json.loads(params["selection"])
                return MagicMock()
            if "SELECT id, turn_index" in q_str:
                res = MagicMock()
                res.mappings.return_value.all.return_value = [
                    {
                        "id": f"t-{p['turn_index']}",
                        "turn_index": p["turn_index"],
                        "status": "PLANNED",
                        "question_version_id": p.get("question_version_id"),
                        "rubric_version_id": p.get("rubric_version_id"),
                        "question_snapshot": json.loads(p["snapshot"]),
                    }
                    for p in inserted_turns
                ]
                return res
            m = MagicMock()
            m.mappings.return_value.all.return_value = []
            m.mappings.return_value.one_or_none.return_value = None
            return m

        db.execute.side_effect = _exec
        session_row = {"id": "sess-tc-11-fixed", "plan_id": "plan-tc-11", "locale": "en-US"}
        return await select_and_freeze_questions(db=db, session_row=session_row), updated_payload

    # Run A: targets [T1, T2], concept-1 candidates [X, Y]
    outA, payloadA = await _run_freeze_pipeline([t1, t2], [cand_x_row, cand_y_row])

    # Run B: targets [T2, T1] (reversed), concept-1 candidates [Y, X] (shuffled)
    outB, payloadB = await _run_freeze_pipeline([t2, t1], [cand_y_row, cand_x_row])

    # Verify identical semantic output
    assert outA["fingerprint"] == outB["fingerprint"]
    assert outA["turnCount"] == outB["turnCount"] == 5
    assert outA["technicalQuestionCount"] == outB["technicalQuestionCount"] == 2
    assert payloadA["questionSelection"]["fingerprint"] == payloadB["questionSelection"]["fingerprint"]

    # Compare all turns
    turnsA = outA["turns"]
    turnsB = outB["turns"]
    assert len(turnsA) == len(turnsB) == 5
    for idx in range(5):
        assert turnsA[idx]["turnIndex"] == turnsB[idx]["turnIndex"]
        assert turnsA[idx]["questionVersionId"] == turnsB[idx]["questionVersionId"]
        assert turnsA[idx]["question"]["questionType"] == turnsB[idx]["question"]["questionType"]
        assert turnsA[idx]["question"]["stage"] == turnsB[idx]["question"]["stage"]


def test_tc_pack_12_three_target_backtracking():
    """TC-PACK-12: Three-Target Backtracking.

    Candidate Domains in Fixture:
    - T1 domain: [B (rel 0.95), A (rel 0.80)] -> prefers B over A
    - T2 domain: [B (rel 0.95), C (rel 0.85)] -> prefers B over C
    - T3 domain: [C (rel 0.95), B (rel 0.80)] -> prefers C over B

    Search & Backtracking Dynamics:
    - If T1 greedily claims its #1 choice (B):
      T2 is left with only [C], so T2 claims C.
      T3 has candidate domain [C, B], but both B and C are already used!
      Forward checking detects an empty compatible domain at T3 (c(T3) = 0).
      Backtracking triggers, undoing T2 -> C and T1 -> B.
    - T1 then explores candidate A.
      Under T1 -> A, both T2 and T3 can be satisfied with {B, C}.
      Two conflict-free assignments exist:
        Assignment 1: T1 -> A, T2 -> B, T3 -> C
        Assignment 2: T1 -> A, T2 -> C, T3 -> B
      Under Global Objective G(A) = (R(T1), R(T2), R(T3), tiebreaker):
        R(T1) is identical (-0.80).
        R(T2) for Assignment 1 is -0.95 (B) vs -0.85 (C) for Assignment 2.
        Since -0.95 < -0.85, Assignment 1 is the unique deterministic optimum.

    Expected Result:
      T1 -> A
      T2 -> B
      T3 -> C
    """
    t1 = {
        "conceptId": "target-1",
        "taxonomyVersion": "internal-2026.1",
        "selectionRank": 1,
        "importance": 1.0,
        "targetArchetype": "TEXT",
        "floorSeconds": 180,
        "timeEnvelopeSeconds": 360,
    }
    t2 = {
        "conceptId": "target-2",
        "taxonomyVersion": "internal-2026.1",
        "selectionRank": 2,
        "importance": 1.0,
        "targetArchetype": "TEXT",
        "floorSeconds": 180,
        "timeEnvelopeSeconds": 360,
    }
    t3 = {
        "conceptId": "target-3",
        "taxonomyVersion": "internal-2026.1",
        "selectionRank": 3,
        "importance": 1.0,
        "targetArchetype": "TEXT",
        "floorSeconds": 180,
        "timeEnvelopeSeconds": 360,
    }

    cand_a = candidate(question_version_id="qv-A", soft_answer_seconds=180, relevance=0.80)
    cand_b = candidate(question_version_id="qv-B", soft_answer_seconds=180, relevance=0.95)
    cand_c = candidate(question_version_id="qv-C", soft_answer_seconds=180, relevance=0.85)

    session_id = "salt-tc-12"
    # For T1: B is ranked before A (relevance 0.95 vs 0.80)
    subsets_t1 = [
        (_subset_objective([cand_b], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id=session_id), [cand_b]),
        (_subset_objective([cand_a], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id=session_id), [cand_a]),
    ]
    # For T2: B is ranked before C (relevance 0.95 vs 0.85)
    cand_b_t2 = candidate(question_version_id="qv-B", soft_answer_seconds=180, relevance=0.95)
    subsets_t2 = [
        (_subset_objective([cand_b_t2], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id=session_id), [cand_b_t2]),
        (_subset_objective([cand_c], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id=session_id), [cand_c]),
    ]
    # For T3: C is ranked before B (relevance 0.95 vs 0.80)
    cand_c_t3 = candidate(question_version_id="qv-C", soft_answer_seconds=180, relevance=0.95)
    cand_b_t3 = candidate(question_version_id="qv-B", soft_answer_seconds=180, relevance=0.80)
    subsets_t3 = [
        (_subset_objective([cand_c_t3], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id=session_id), [cand_c_t3]),
        (_subset_objective([cand_b_t3], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id=session_id), [cand_b_t3]),
    ]

    domains = {
        "internal-2026.1:target-1": subsets_t1,
        "internal-2026.1:target-2": subsets_t2,
        "internal-2026.1:target-3": subsets_t3,
    }

    metrics: dict[str, Any] = {}
    assignment = _solve_global_question_assignment(
        targets=[t1, t2, t3],
        target_domains=domains,
        session_id=session_id,
        metrics=metrics,
    )

    assert assignment is not None
    assert [q.question_version_id for q in assignment["internal-2026.1:target-1"]] == ["qv-A"]
    assert [q.question_version_id for q in assignment["internal-2026.1:target-2"]] == ["qv-B"]
    assert [q.question_version_id for q in assignment["internal-2026.1:target-3"]] == ["qv-C"]

    # Verify backtracking actually occurred
    assert metrics.get("backtrack_count", 0) >= 1
    assert metrics.get("global_search_nodes", 0) >= 3


@pytest.mark.asyncio
async def test_turn_count_matches_actual_persisted_turns():
    """Verify turnCount in questionSelection matches actual persisted turns (K + 3)."""
    db = AsyncMock()
    plan_result = MagicMock()
    plan_result.mappings.return_value.one_or_none.return_value = {
        "status": "READY",
        "plan_payload": {
            "policyVersion": "interview-planner-v2-dynamic",
            "targets": [
                {
                    "conceptId": "concept-single",
                    "label": "Single Target",
                    "targetArchetype": "TEXT",
                    "floorSeconds": 180,
                    "timeEnvelopeSeconds": 360,
                    "taxonomyVersion": "internal-2026.1",
                    "selectionRank": 1,
                    "importance": 1.0,
                },
            ],
        },
    }
    candidate_row = {
        "stable_key": "k-1",
        "question_version_id": "qv-1",
        "version": "1.0",
        "status": "APPROVED",
        "question_type": "technical",
        "difficulty_band": "intermediate",
        "canonical_locale": "en-US",
        "canonical_text": "Technical Question",
        "objective": "Assess",
        "thinking_seconds": 0,
        "soft_answer_seconds": 180,
        "hard_answer_seconds": 240,
        "mapping_purpose": "PRIMARY_COMPETENCY",
        "relevance": 0.9,
        "rubric_version_id": "rv-1",
    }

    inserted_turns = []
    updated_plan_payload = {}

    async def _execute_mock(query, params=None):
        q_str = str(query)
        if "SELECT status, plan_payload" in q_str:
            return plan_result
        if "FROM interview_questions" in q_str:
            res = MagicMock()
            res.mappings.return_value.all.return_value = [candidate_row]
            return res
        if "rubric_versions" in q_str:
            res = MagicMock()
            res.mappings.return_value.one_or_none.return_value = {
                "id": "rv-1",
                "version": "1.0",
                "score_min": 0,
                "score_max": 3,
                "minimum_coverage": 0.6,
                "aggregation_method": "weighted_mean",
                "aggregation_policy": {},
            }
            return res
        if "INSERT INTO interview_turns" in q_str:
            inserted_turns.append(params)
            return MagicMock()
        if "UPDATE interview_session_plans" in q_str:
            nonlocal updated_plan_payload
            selection_raw = json.loads(params["selection"])
            updated_plan_payload = selection_raw
            return MagicMock()
        if "SELECT id, turn_index" in q_str:
            res = MagicMock()
            res.mappings.return_value.all.return_value = [
                {"id": f"t-{i}", "turn_index": i, "status": "PLANNED", "question_version_id": None, "rubric_version_id": None, "question_snapshot": {}}
                for i in range(len(inserted_turns))
            ]
            return res
        m = MagicMock()
        m.mappings.return_value.all.return_value = []
        m.mappings.return_value.one_or_none.return_value = None
        return m

    db.execute.side_effect = _execute_mock

    session_row = {"id": "sess-tc-turncount", "plan_id": "plan-tc-turncount", "locale": "en-US"}
    result = await select_and_freeze_questions(db=db, session_row=session_row)

    # 1 technical question frozen (K=1)
    # Total turns: Turn 0 (WARM_UP), Turn 1 (VALIDATE), Turn 2 (technical), Turn 3 (BEHAVIORAL) = 4 turns
    assert result["technicalQuestionCount"] == 1
    assert result["turnCount"] == 4
    assert len(inserted_turns) == 4
    assert updated_plan_payload["questionSelection"]["turnCount"] == 4
    assert updated_plan_payload["questionSelection"]["technicalQuestionCount"] == 1


def test_tc_pack_13_first_feasible_is_global_optimum():
    """TC-PACK-13: First Feasible is Global Optimum.

    Fixture:
      T1: A (rank 1), B (rank 2)
      T2: C (rank 1), D (rank 2)
      No conflicts between candidates.

    Expected:
      - Assignment: T1 -> A, T2 -> C
      - Returns immediately upon first complete assignment
      - Search nodes = 2 (does NOT enumerate A+D, B+C, B+D)
      - Backtracks = 0
    """
    t1 = {
        "conceptId": "concept-1",
        "taxonomyVersion": "internal-2026.1",
        "selectionRank": 1,
        "importance": 1.0,
        "targetArchetype": "TEXT",
        "floorSeconds": 180,
        "timeEnvelopeSeconds": 360,
    }
    t2 = {
        "conceptId": "concept-2",
        "taxonomyVersion": "internal-2026.1",
        "selectionRank": 2,
        "importance": 1.0,
        "targetArchetype": "TEXT",
        "floorSeconds": 180,
        "timeEnvelopeSeconds": 360,
    }

    cand_a = candidate(question_version_id="qv-A", soft_answer_seconds=180, relevance=0.95)
    cand_b = candidate(question_version_id="qv-B", soft_answer_seconds=180, relevance=0.80)
    cand_c = candidate(question_version_id="qv-C", soft_answer_seconds=180, relevance=0.95)
    cand_d = candidate(question_version_id="qv-D", soft_answer_seconds=180, relevance=0.80)

    session_id = "salt-tc-13"
    subsets_t1 = [
        (_subset_objective([cand_a], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id=session_id), [cand_a]),
        (_subset_objective([cand_b], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id=session_id), [cand_b]),
    ]
    subsets_t2 = [
        (_subset_objective([cand_c], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id=session_id), [cand_c]),
        (_subset_objective([cand_d], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id=session_id), [cand_d]),
    ]

    domains = {
        "internal-2026.1:concept-1": subsets_t1,
        "internal-2026.1:concept-2": subsets_t2,
    }

    metrics: dict[str, Any] = {}
    assignment = _solve_global_question_assignment(
        targets=[t1, t2],
        target_domains=domains,
        session_id=session_id,
        metrics=metrics,
    )

    assert assignment is not None
    assert [q.question_version_id for q in assignment["internal-2026.1:concept-1"]] == ["qv-A"]
    assert [q.question_version_id for q in assignment["internal-2026.1:concept-2"]] == ["qv-C"]

    # Verify search stopped immediately at first feasible assignment (A+C)
    # Search nodes should be exactly 2 (node for A, node for C), NOT enumerating A+D, B+C, B+D
    assert metrics.get("global_search_nodes") == 2
    assert metrics.get("backtrack_count") == 0


def test_tc_pack_14_early_best_branch_impossible():
    """TC-PACK-14: Early Best Branch Impossible (Forward-Checking Backtrack).

    Fixture:
      T1: A (rank 1), B (rank 2)
      T2: A only (rank 1)

    Dynamics:
      - DFS tries T1 -> A.
      - Forward checking checks T2: T2 has only A, which conflicts with T1 -> A.
      - Forward checking fails immediately, triggering backtrack before recursing into T2.
      - DFS tries T1 -> B.
      - Forward checking checks T2: T2 has A, which is disjoint from B.
      - T2 assigns A.
      - Success: T1 -> B, T2 -> A.

    Expected:
      - Assignment: T1 -> B, T2 -> A
      - Backtrack count >= 1
      - Search nodes = 3 (T1=A, T1=B, T2=A)
    """
    t1 = {
        "conceptId": "concept-1",
        "taxonomyVersion": "internal-2026.1",
        "selectionRank": 1,
        "importance": 1.0,
        "targetArchetype": "TEXT",
        "floorSeconds": 180,
        "timeEnvelopeSeconds": 360,
    }
    t2 = {
        "conceptId": "concept-2",
        "taxonomyVersion": "internal-2026.1",
        "selectionRank": 2,
        "importance": 1.0,
        "targetArchetype": "TEXT",
        "floorSeconds": 180,
        "timeEnvelopeSeconds": 360,
    }

    cand_a = candidate(question_version_id="qv-A", soft_answer_seconds=180, relevance=0.95)
    cand_b = candidate(question_version_id="qv-B", soft_answer_seconds=180, relevance=0.80)

    session_id = "salt-tc-14"
    subsets_t1 = [
        (_subset_objective([cand_a], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id=session_id), [cand_a]),
        (_subset_objective([cand_b], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id=session_id), [cand_b]),
    ]
    subsets_t2 = [
        (_subset_objective([cand_a], time_envelope_seconds=360, difficulty="intermediate", locale="en-US", session_id=session_id), [cand_a]),
    ]

    domains = {
        "internal-2026.1:concept-1": subsets_t1,
        "internal-2026.1:concept-2": subsets_t2,
    }

    metrics: dict[str, Any] = {}
    assignment = _solve_global_question_assignment(
        targets=[t1, t2],
        target_domains=domains,
        session_id=session_id,
        metrics=metrics,
    )

    assert assignment is not None
    assert [q.question_version_id for q in assignment["internal-2026.1:concept-1"]] == ["qv-B"]
    assert [q.question_version_id for q in assignment["internal-2026.1:concept-2"]] == ["qv-A"]

    assert metrics.get("backtrack_count", 0) >= 1
    assert metrics.get("global_search_nodes", 0) == 3
