"""The strict score must not move when a second metric is published alongside it.

``profile_match_score`` is meant to be purely additive: it reads a value scoring
already computed and publishes it, without touching eligibility, weighting or
suitability. "Purely additive" is easy to claim and easy to break, so this file
pins the whole strict subtree - every requirement result, factor, weight and
provenance field - as a fingerprint per scenario.

The expected values were frozen from the tree BEFORE the second metric existed.
If a fingerprint changes, the change was not additive, regardless of what the
headline suitability number says.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from src.modules.matching.application.facade import MatchingFacade
from src.modules.matching.domain.schemas import MatchRequest, MatchResult

from tests.modules.matching.test_matching_pipeline import StubEmbedder, _payload

# Every field that existed before the profile-match metric. An explicit
# allowlist, so fields added later cannot quietly enter the fingerprint and
# make a real regression look like an expected change.
_STRICT_KEYS = (
    "schemaVersion",
    "pipelineVersion",
    "eligibility",
    "compatibilityStatus",
    "diagnosticScore",
    "suitabilityScore",
    "fitBand",
    "decision",
    "failedMustHaveRequirements",
    "requirementResults",
    "compatibilityResults",
    "factorResults",
    "scoreProvenance",
    "warnings",
)


def _strict_fingerprint(result: MatchResult) -> str:
    """SHA-256 over every pre-existing field, excluding anything added since."""
    payload = json.loads(result.model_dump_json(by_alias=True))
    strict = {key: payload[key] for key in _STRICT_KEYS}
    canonical = json.dumps(strict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _run(**kwargs) -> MatchResult:
    return MatchingFacade(StubEmbedder()).match(MatchRequest.model_validate(_payload(**kwargs)))


# Scenarios chosen to cover each distinct path that produces (or withholds)
# a suitability score: satisfied must-have, must-have failed on duration,
# must-have absent from the CV entirely.
_SCENARIOS = {
    "eligible_scored": {},
    "ineligible_insufficient_duration": {"java_months": 12},
    "ineligible_skill_absent": {"add_java": False},
    "eligible_no_duration_claim": {"java_months": None},
}

_EXPECTED_FINGERPRINT = {
    "eligible_scored": "802f654f487db57d88e7a4a25811a5f70a61543f05b245a00cd3a9002c86aa97",
    "ineligible_insufficient_duration": "21e5978de04fa1834a0a3df97abbca0a2a4fe2b400de950fed2e0b0c95b9aa07",
    "ineligible_skill_absent": "8ceebb02c90088fafb63a2a1c48544f96fbb18f92969fc650fec1ab1ebe08eb8",
    "eligible_no_duration_claim": "387a464067e6eded35905b95b46263ace798c66fc01e01542902d031dfa7055f",
}

# Re-pinned 2026-11-10. The earlier numbers were produced by a defect, not by
# this fixture changing: these resumes are synthetic and have no row in
# user_cvs, and ParadeDB answered "no matching row" with a sparse score of 0.0
# - a confident "shares nothing with the job" for a document it never searched.
# The index now reports not-indexed as unknown, so semantic falls back to the
# embedding half alone (reliability 0.85, sparseScore None). These values no
# longer depend on whether Postgres is reachable: unreachable and not-indexed
# both resolve to the same unknown.
# Readable companions to the fingerprints: when one of these fails the message
# names the number that moved, instead of only reporting a hash mismatch.
_EXPECTED_VERDICT = {
    "eligible_scored": ("eligible", "strong_fit", "assessed", 1.0, 1.0),
    "ineligible_insufficient_duration": ("ineligible", "not_eligible", "assessed", None, 0.32210947515634997),
    "ineligible_skill_absent": ("ineligible", "not_eligible", "assessed", None, 0.32210947515634997),
    "eligible_no_duration_claim": ("review_required", "review_required", "abstained", None, 0.6271602113359924),
}


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_strict_verdict_and_scores_are_unchanged(name: str) -> None:
    result = _run(**_SCENARIOS[name])
    expected = _EXPECTED_VERDICT[name]
    # Exact equality on purpose: approx would hide the float drift this file exists to rule out.
    assert (
        result.eligibility,
        result.fit_band,
        result.decision,
        result.suitability_score,
        result.diagnostic_score,
    ) == expected


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_strict_result_subtree_is_unchanged(name: str) -> None:
    """The entire pre-existing result subtree is byte-identical, not just the score."""
    result = _run(**_SCENARIOS[name])
    assert _strict_fingerprint(result) == _EXPECTED_FINGERPRINT[name], (
        f"scenario {name!r}: the strict subtree changed. "
        "Publishing a second metric must not alter requirement results, factor "
        "weights, provenance or the suitability score."
    )
