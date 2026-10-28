"""Ngưỡng điểm matching để vào phòng phỏng vấn (planning/entry_gate.py)."""

from __future__ import annotations

import importlib

import pytest
from fastapi import HTTPException

from src.modules.interviews.planning import planner as planner_module
from src.modules.interviews.planning.entry_gate import (
    DEFAULT_MIN_MATCH_PERCENT,
    EntryPolicy,
    InterviewEntryBlocked,
    entry_policy,
    evaluate_entry,
    fit_adjusted_difficulty,
    match_display_percentage,
)
from src.modules.matching.schemas import MatchResult

# The package re-exports the APIRouter object as `router`; patch the module.
router_module = importlib.import_module("src.modules.interviews.api.router")


def _match(**overrides) -> MatchResult:
    data = {
        "resumeId": "cv-1",
        "jobId": "job-1",
        "policyVersion": "balanced-v1",
        "eligibility": "eligible",
        "suitabilityScore": 0.72,
        "diagnosticScore": None,
        "fitBand": "partial_fit",
        "decision": "assessed",
        "requirementResults": [],
        "factorResults": [],
    }
    data.update(overrides)
    return MatchResult(**data)


# Screening mode: every rule blocks. The default (practice mode) blocks none.
POLICY = EntryPolicy(min_match_percent=40, block_not_eligible=True, require_match_score=True)
PRACTICE = EntryPolicy(min_match_percent=0, block_not_eligible=False, require_match_score=False)


def test_percentage_matches_what_the_fit_report_shows() -> None:
    assert match_display_percentage(_match()) == 72
    # Not a final score (review required): the report shows the diagnostic score.
    assert match_display_percentage(
        _match(eligibility="review_required", fitBand="review_required", decision="abstained",
               suitabilityScore=0.9, diagnosticScore=0.35)
    ) == 35
    assert match_display_percentage(
        _match(eligibility="review_required", fitBand="insufficient_evidence", decision="abstained",
               suitabilityScore=None, diagnosticScore=None)
    ) is None


def test_score_at_or_above_threshold_enters() -> None:
    assert evaluate_entry(_match(suitabilityScore=0.40), POLICY).allowed
    assert evaluate_entry(_match(suitabilityScore=0.95, fitBand="strong_fit"), POLICY).allowed


def test_score_below_threshold_is_blocked() -> None:
    decision = evaluate_entry(_match(suitabilityScore=0.39), POLICY)
    assert not decision.allowed
    assert decision.reason == "match_below_threshold"
    detail = decision.detail()
    assert detail["errorCode"] == "match_below_threshold"
    assert detail["details"] == {
        "percentage": 39, "minMatchPercent": 40, "fitBand": "partial_fit", "eligibility": "eligible",
    }
    assert "39%" in detail["message"] and "40%" in detail["message"]


def test_failed_must_have_is_blocked_even_with_a_high_diagnostic_score() -> None:
    decision = evaluate_entry(
        _match(eligibility="ineligible", fitBand="not_eligible", suitabilityScore=None, diagnosticScore=0.8),
        POLICY,
    )
    assert decision.reason == "match_not_eligible"


def test_no_score_is_blocked() -> None:
    decision = evaluate_entry(
        _match(eligibility="review_required", fitBand="insufficient_evidence", decision="abstained",
               suitabilityScore=None),
        POLICY,
    )
    assert decision.reason == "match_score_unavailable"


def test_each_rule_can_be_turned_off(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEW_MIN_MATCH_PERCENT", "0")
    monkeypatch.setenv("INTERVIEW_BLOCK_NOT_ELIGIBLE", "0")
    monkeypatch.setenv("INTERVIEW_REQUIRE_MATCH_SCORE", "false")
    policy = entry_policy()
    assert policy == PRACTICE
    assert evaluate_entry(_match(suitabilityScore=0.05)).allowed
    assert evaluate_entry(_match(eligibility="ineligible", fitBand="not_eligible", suitabilityScore=None)).allowed


def test_default_is_practice_mode(monkeypatch) -> None:
    for name in (
        "INTERVIEW_MIN_MATCH_PERCENT", "INTERVIEW_BLOCK_NOT_ELIGIBLE",
        "INTERVIEW_REQUIRE_MATCH_SCORE", "INTERVIEW_LOW_FIT_PERCENT",
    ):
        monkeypatch.delenv(name, raising=False)
    policy = entry_policy()
    assert policy == EntryPolicy(0, False, False, 40)
    # Nobody with a match result is blocked; the far-off cases only get warnings.
    low = evaluate_entry(_match(suitabilityScore=0.12))
    assert low.allowed and low.warnings == ("match_low_fit",)
    failed = evaluate_entry(_match(eligibility="ineligible", fitBand="not_eligible", suitabilityScore=None,
                                   diagnosticScore=0.3))
    assert failed.allowed and failed.warnings == ("match_not_eligible", "match_low_fit")
    unscored = evaluate_entry(_match(eligibility="review_required", fitBand="insufficient_evidence",
                                     decision="abstained", suitabilityScore=None))
    assert unscored.allowed and unscored.warnings == ("match_score_unavailable",)
    assert evaluate_entry(_match(suitabilityScore=0.72)).warnings == ()


def test_policy_bad_values(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEW_MIN_MATCH_PERCENT", "abc")
    assert entry_policy().min_match_percent == DEFAULT_MIN_MATCH_PERCENT
    monkeypatch.setenv("INTERVIEW_MIN_MATCH_PERCENT", "150")
    assert entry_policy().min_match_percent == 100
    assert entry_policy().to_payload()["minMatchPercent"] == 100


@pytest.mark.asyncio
async def test_session_is_not_created_below_threshold(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEW_MIN_MATCH_PERCENT", "40")

    async def low_match(**_kwargs):
        return _match(suitabilityScore=0.2)

    monkeypatch.setattr(router_module, "evaluate_session_match", low_match)
    with pytest.raises(HTTPException) as caught:
        await router_module._require_match_threshold(None, {"sub": "u1"}, "cv-1", "job-1")
    assert caught.value.status_code == 409
    assert caught.value.detail["errorCode"] == "match_below_threshold"


@pytest.mark.asyncio
async def test_session_is_created_at_threshold(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEW_MIN_MATCH_PERCENT", "40")

    async def ok_match(**_kwargs):
        return _match(suitabilityScore=0.4)

    monkeypatch.setattr(router_module, "evaluate_session_match", ok_match)
    await router_module._require_match_threshold(None, {"sub": "u1"}, "cv-1", "job-1")


@pytest.mark.asyncio
async def test_unparsed_cv_has_no_score_to_enter_with(monkeypatch) -> None:
    async def not_parsed(**_kwargs):
        raise ValueError("CV canonical parsing is incomplete")

    monkeypatch.setattr(router_module, "evaluate_session_match", not_parsed)
    with pytest.raises(HTTPException) as caught:
        await router_module._require_match_threshold(None, {"sub": "u1"}, "cv-1", "job-1")
    assert caught.value.status_code == 409
    assert caught.value.detail["errorCode"] == "match_score_unavailable"


@pytest.mark.asyncio
async def test_plan_is_not_built_for_a_session_below_threshold(monkeypatch) -> None:
    """A session created before the threshold existed (or was raised) must not open."""

    async def fake_resume(db, *, user_id, resume_id):
        return object(), None

    async def fake_job(db, *, user, job_id):
        return object()

    async def low_match(_payload):
        return _match(suitabilityScore=0.1)

    monkeypatch.setenv("INTERVIEW_MIN_MATCH_PERCENT", "40")
    monkeypatch.setattr(planner_module, "_load_resume", fake_resume)
    monkeypatch.setattr(planner_module, "_load_job", fake_job)
    monkeypatch.setattr(planner_module, "MatchRequest", lambda **kw: kw)
    monkeypatch.setattr(planner_module, "evaluate_match", low_match)
    with pytest.raises(InterviewEntryBlocked) as caught:
        await planner_module.build_and_persist_session_plan(
            db=None,
            user={"sub": "u1"},
            session_row={"id": "s1", "plan_id": "p1", "plan_status": "DRAFT", "resume_id": "cv-1", "job_id": "job-1"},
        )
    assert caught.value.decision.reason == "match_below_threshold"


def test_practice_tier_follows_the_score() -> None:
    tier = lambda **kw: evaluate_entry(_match(**kw), PRACTICE).tier  # noqa: E731
    assert tier(suitabilityScore=0.39) == "basic"
    assert tier(eligibility="ineligible", fitBand="not_eligible", suitabilityScore=None, diagnosticScore=0.9) == "basic"
    assert tier(suitabilityScore=0.40) == "standard"
    assert tier(suitabilityScore=0.69) == "standard"
    assert tier(suitabilityScore=0.70) == "challenge"
    assert tier(suitabilityScore=0.95, fitBand="strong_fit") == "challenge"
    # A high diagnostic score is not "đủ điều kiện": no challenge without the final score.
    assert tier(eligibility="review_required", fitBand="review_required", decision="abstained",
                suitabilityScore=None, diagnosticScore=0.85) == "standard"
    assert tier(eligibility="review_required", fitBand="insufficient_evidence", decision="abstained",
                suitabilityScore=None) == "standard"


def test_challenge_threshold_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEW_CHALLENGE_PERCENT", "80")
    assert entry_policy().challenge_percent == 80
    assert evaluate_entry(_match(suitabilityScore=0.75)).tier == "standard"
    assert entry_policy().to_payload()["challengePercent"] == 80


def test_difficulty_moves_one_level_by_tier() -> None:
    job = {"level": "intermediate", "source": "job_seniority", "seniority": "mid"}
    low = evaluate_entry(_match(suitabilityScore=0.2), PRACTICE)
    eased = fit_adjusted_difficulty(job, low)
    assert eased["level"] == "foundational" and eased["jobLevel"] == "intermediate"
    assert eased["practiceTier"] == "basic"
    assert eased["fitAdjustment"] == {"direction": "easier", "reasons": ["match_low_fit"], "matchPercent": 20}

    high = evaluate_entry(_match(suitabilityScore=0.82, fitBand="strong_fit"), PRACTICE)
    harder = fit_adjusted_difficulty(job, high)
    assert harder["level"] == "advanced" and harder["jobLevel"] == "intermediate"
    assert harder["fitAdjustment"]["direction"] == "harder"

    standard = evaluate_entry(_match(suitabilityScore=0.55), PRACTICE)
    assert fit_adjusted_difficulty(job, standard) == {**job, "practiceTier": "standard"}
    # Bounded: never below foundational, never above advanced.
    assert fit_adjusted_difficulty({"level": "foundational"}, low)["level"] == "foundational"
    assert fit_adjusted_difficulty({"level": "advanced"}, high)["level"] == "advanced"


@pytest.mark.asyncio
async def test_practice_mode_creates_the_session_for_a_low_score(monkeypatch) -> None:
    for name in ("INTERVIEW_MIN_MATCH_PERCENT", "INTERVIEW_BLOCK_NOT_ELIGIBLE", "INTERVIEW_REQUIRE_MATCH_SCORE"):
        monkeypatch.delenv(name, raising=False)

    async def far_match(**_kwargs):
        return _match(eligibility="ineligible", fitBand="not_eligible", suitabilityScore=None, diagnosticScore=0.1)

    monkeypatch.setattr(router_module, "evaluate_session_match", far_match)
    await router_module._require_match_threshold(None, {"sub": "u1"}, "cv-1", "job-1")
