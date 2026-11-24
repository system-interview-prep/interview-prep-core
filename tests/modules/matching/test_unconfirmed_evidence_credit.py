"""A cited-but-unconfirmed passage is paid for its fit, not for existing.

The credit used to be a flat constant handed to any ``unknown`` that cited any
passage.  Retrieval returns its best candidates whether or not any of them is
about the requirement, so the constant was paid without the passage ever being
looked at - and a weighted mean containing a constant drifts toward it.
"""

import pytest

from src.modules.matching.application.facade import (
    _UNCONFIRMED_EVIDENCE_CREDIT,
    MatchingFacade,
    _unconfirmed_credit,
)
from src.modules.matching.domain.schemas import (
    EvidenceCandidateTrace,
    RequirementResult,
    UnresolvedRequirement,
)


def requirement(requirement_id: str, priority: str = "must_have") -> UnresolvedRequirement:
    return UnresolvedRequirement(
        requirementId=requirement_id,
        type="unresolved",
        kind="experience",
        priority=priority,
        sourceEvidenceRef="jd-1",
        rawLabel=requirement_id,
    )


def candidate(evidence_ref: str, semantic_score: float, rank: int = 1):
    return EvidenceCandidateTrace(
        evidenceRef=evidence_ref,
        retrievalMethod="semantic_lexical_expansion",
        lexicalScore=0.0,
        semanticScore=semantic_score,
        rank=rank,
    )


def unknown(requirement_id: str, *, cites: list[str], traces=()) -> RequirementResult:
    return RequirementResult(
        requirementId=requirement_id,
        status="unknown",
        score=None,
        confidence=0.4,
        evidenceRefs=cites,
        reasonCode="concept_semantic_evidence_weak",
        retrievalCandidates=list(traces),
    )


def settled(requirement_id: str, status: str, score: float, cites: list[str]) -> RequirementResult:
    return RequirementResult(
        requirementId=requirement_id,
        status=status,
        score=score,
        confidence=0.8,
        evidenceRefs=cites,
        reasonCode="concept_evidence_sufficient",
    )


class TestCreditFollowsTheFit:
    def test_a_passage_that_fits_well_earns_near_the_ceiling(self):
        result = unknown("req-1", cites=["ev-1"], traces=[candidate("ev-1", 0.9)])
        assert _unconfirmed_credit(result) == pytest.approx(0.45 * 0.9)

    def test_a_passage_that_barely_fits_earns_little(self):
        result = unknown("req-1", cites=["ev-1"], traces=[candidate("ev-1", 0.1)])
        assert _unconfirmed_credit(result) == pytest.approx(0.45 * 0.1)

    def test_the_ceiling_is_never_exceeded(self):
        result = unknown("req-1", cites=["ev-1"], traces=[candidate("ev-1", 1.0)])
        assert _unconfirmed_credit(result) == pytest.approx(_UNCONFIRMED_EVIDENCE_CREDIT)

    def test_the_best_cited_passage_decides(self):
        result = unknown(
            "req-1",
            cites=["ev-1", "ev-2"],
            traces=[candidate("ev-1", 0.2), candidate("ev-2", 0.8, rank=2)],
        )
        assert _unconfirmed_credit(result) == pytest.approx(0.45 * 0.8)

    def test_a_passage_the_requirement_did_not_cite_is_ignored(self):
        """A strong candidate that did not make it into the citation is not
        evidence for this requirement."""
        result = unknown(
            "req-1", cites=["ev-1"], traces=[candidate("ev-1", 0.2), candidate("ev-9", 0.95, rank=2)]
        )
        assert _unconfirmed_credit(result) == pytest.approx(0.45 * 0.2)


class TestTheUntracedPathIsUnchanged:
    """A known residue, written down rather than fixed.

    The targeted LLM search and self-reported answers cite a passage without a
    retrieval score behind it.  There is nothing to scale by, so those keep the
    flat ceiling and still drift toward it.
    """

    def test_an_untraced_citation_keeps_the_flat_ceiling(self):
        result = unknown("req-1", cites=["ev-1"])
        assert _unconfirmed_credit(result) == _UNCONFIRMED_EVIDENCE_CREDIT


class TestMr2AddingAnUnmeetableRequirement:
    """Adding a requirement nobody can meet must not raise coverage.

    Measured on the frozen corpus before this change: the probe landed
    ``unknown`` with a citation, took the flat 0.45, and lifted coverage on
    jobscan_0100 (37.32 -> 38.44) and jobscan_0850 (43.64 -> 43.71) - every
    pair sitting below the constant.  The arrangement below is that shape: a
    pair at 0.400, and a probe whose cited passage scores 0.1.
    """

    @staticmethod
    def coverage(pairs) -> float:
        return MatchingFacade._requirement_coverage_factor(0.6, pairs).raw_score

    def pairs(self):
        return [
            (requirement("req-a"), settled("req-a", "met", 0.8, ["ev-1"])),
            (requirement("req-b"), settled("req-b", "not_met", 0.0, [])),
        ]

    def test_the_pair_is_where_the_corpus_put_it(self):
        assert self.coverage(self.pairs()) == pytest.approx(0.4)

    def test_a_weakly_cited_probe_does_not_raise_coverage(self):
        before = self.coverage(self.pairs())
        probe = (
            requirement("req-probe"),
            unknown("req-probe", cites=["ev-9"], traces=[candidate("ev-9", 0.1)]),
        )
        after = self.coverage([*self.pairs(), probe])
        assert after < before

    def test_the_flat_constant_would_have_raised_it(self):
        """The counterfactual, so the test states what it is protecting."""
        before = self.coverage(self.pairs())
        assert _UNCONFIRMED_EVIDENCE_CREDIT > before
