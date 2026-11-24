"""The second published metric: profile match, of equal standing with suitability.

It answers "how much of this posting is reflected in this CV" rather than "does
this CV satisfy the stated requirements", so unlike suitability it is published
for every pair - including the rejected ones, where suitability is withheld.
"""

from __future__ import annotations

import pytest

from src.modules.matching.application.facade import MatchingFacade
from src.modules.matching.domain.schemas import FactorResult, MatchRequest, MatchResult

from tests.modules.matching.test_matching_pipeline import StubEmbedder, _payload


def _semantic(result: MatchResult) -> FactorResult:
    return next(item for item in result.factor_results if item.factor == "semantic")


def _run(**kwargs) -> MatchResult:
    return MatchingFacade(StubEmbedder()).match(MatchRequest.model_validate(_payload(**kwargs)))


def test_profile_match_is_the_semantic_factor_score_unblended() -> None:
    """Exact equality is the contract: read from the factor, never recomputed."""
    result = _run()
    assert result.profile_match_score == _semantic(result).raw_score


def test_profile_match_is_published_for_a_rejected_pair() -> None:
    """The whole point: a pair the gate rejects still gets a profile number.

    Also proves publishing it did not loosen the gate - suitability stays None.
    """
    result = _run(java_months=12)
    assert result.eligibility == "ineligible"
    assert result.suitability_score is None
    assert result.profile_match_score is not None
    assert result.profile_match_band is not None


def test_profile_match_is_published_when_the_pair_goes_to_review() -> None:
    result = _run(java_months=None)
    assert result.eligibility == "review_required"
    assert result.suitability_score is None
    assert result.profile_match_score is not None


def test_profile_match_band_agrees_with_the_published_score() -> None:
    """The two fields are derived together, so they cannot drift apart."""
    result = _run()
    assert (result.profile_match_score, result.profile_match_band) == MatchingFacade._profile_match(
        result.factor_results
    )


@pytest.mark.parametrize(
    ("score", "band"),
    [
        (0.0, "below_typical"),
        (0.1979, "below_typical"),
        (0.198, "typical"),  # p25 boundary is inclusive of the upper band
        (0.279, "typical"),  # the measured median
        (0.3519, "typical"),
        (0.352, "above_typical"),  # p75 boundary likewise
        (0.583, "above_typical"),  # the observed maximum
        (1.0, "above_typical"),
    ],
)
def test_profile_match_band_follows_the_measured_quartiles(score: float, band: str) -> None:
    factors = [
        FactorResult(
            factor="semantic",
            status="scored",
            rawScore=score,
            reliability=1.0,
            policyWeight=0.15,
            effectiveWeight=0.15,
        )
    ]
    assert MatchingFacade._profile_match(factors) == (score, band)


def test_profile_match_is_none_when_the_semantic_factor_did_not_score() -> None:
    """The one legitimate null, and it carries no band either."""
    factors = [
        FactorResult(
            factor="semantic",
            status="unknown",
            reliability=0.0,
            policyWeight=0.15,
            effectiveWeight=0.0,
            warningCode="semantic_scorer_unavailable",
        )
    ]
    assert MatchingFacade._profile_match(factors) == (None, None)


def test_profile_match_ignores_other_factors() -> None:
    """Requirement coverage must not leak into it - that is the blend we rejected."""
    factors = [
        FactorResult(
            factor="requirement_coverage",
            status="scored",
            rawScore=1.0,
            reliability=1.0,
            policyWeight=0.6,
            effectiveWeight=0.8,
        ),
        FactorResult(
            factor="semantic",
            status="scored",
            rawScore=0.25,
            reliability=1.0,
            policyWeight=0.15,
            effectiveWeight=0.2,
        ),
    ]
    assert MatchingFacade._profile_match(factors) == (0.25, "typical")


def test_profile_match_round_trips_through_cache_serialisation() -> None:
    """Both fields survive the dump/validate cycle the match cache uses."""
    result = _run()
    restored = MatchResult.model_validate_json(result.model_dump_json(by_alias=True))
    assert restored.profile_match_score == result.profile_match_score
    assert restored.profile_match_band == result.profile_match_band


def test_profile_match_is_exposed_under_its_camel_case_alias() -> None:
    payload = _run().model_dump(by_alias=True)
    assert "profileMatchScore" in payload
    assert "profileMatchBand" in payload


@pytest.mark.asyncio
async def test_async_and_sync_publish_the_same_profile_match() -> None:
    """Guards the one real risk of two hand-written constructors: divergence.

    The BM25 provider is pinned because the async path otherwise resolves a
    different one (ParadeDB cannot run inside a live event loop and falls back
    to in-memory), which would change the sparse term and compare two different
    semantic scores rather than two code paths.
    """
    payload = _payload()
    payload["matchingPolicy"] = {"bm25ProviderMode": "in_memory"}
    request = MatchRequest.model_validate(payload)
    sync_result = MatchingFacade(StubEmbedder()).match(request)
    async_result = await MatchingFacade(StubEmbedder()).match_async(request)
    assert sync_result.profile_match_score == async_result.profile_match_score
    assert sync_result.profile_match_band == async_result.profile_match_band
