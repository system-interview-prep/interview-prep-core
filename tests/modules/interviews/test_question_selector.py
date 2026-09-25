from src.modules.interviews.question_selector import (
    _Candidate,
    _allowed_purposes,
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
    exact = candidate(question_version_id="exact", canonical_locale="vi-VN", difficulty_band="intermediate")
    language_only = candidate(question_version_id="lang", canonical_locale="vi", difficulty_band="intermediate")
    harder = candidate(question_version_id="hard", canonical_locale="vi-VN", difficulty_band="advanced")

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
