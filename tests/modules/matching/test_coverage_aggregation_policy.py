"""End to end: the coverage policy changes what the pair scores, and why.

The default policy is untouched, so a frozen baseline stays comparable; the
coverage policy is what a measured run turns on.
"""

import pytest

from src.modules.matching.application.facade import MatchingFacade
from src.modules.matching.domain.schemas import MatchRequest

SHA256 = "c" * 64
CV_TEXT = "Built a Java backend service handling payment settlement at ACME"
JD_TEXT = "Requirements"


class StubEmbedder:
    """No dense half: the coverage path must still score on sparse alone."""

    def embed(self, *_args, **_kwargs):
        raise RuntimeError("embeddings disabled for this test")


def _unresolved(requirement_id: str, label: str, priority: str = "must_have") -> dict:
    return {
        "requirementId": requirement_id,
        "type": "unresolved",
        "kind": "other",
        "priority": priority,
        "sourceEvidenceRef": "jd-ev-1",
        "rawLabel": label,
        "atomicConcepts": [],
    }


def _request(*labels: str, aggregation: str = "document", spans: int = 1) -> MatchRequest:
    evidence = []
    offset = 0
    for index in range(spans):
        text = CV_TEXT if index == 0 else f"{CV_TEXT} ({index})"
        evidence.append(
            {
                "evidenceId": f"cv-ev-{index}",
                "documentId": "cv-doc-1",
                "documentSha256": SHA256,
                "section": "experience",
                "text": text,
                "charStart": offset,
                "charEnd": offset + len(text),
            }
        )
        offset += len(text)
    return MatchRequest.model_validate(
        {
            "schemaVersion": "2.1",
            "resume": {
                "schemaVersion": "2.1",
                "resumeId": "cv-1",
                "documentId": "cv-doc-1",
                "documentSha256": SHA256,
                "rawText": "".join(item["text"] for item in evidence),
                "skills": [],
                "evidence": evidence,
            },
            "job": {
                "schemaVersion": "2.1",
                "jobId": "job-1",
                "documentId": "job-doc-1",
                "documentSha256": SHA256,
                "jobTitle": "Backend Developer",
                "evidence": [
                    {
                        "evidenceId": "jd-ev-1",
                        "documentId": "job-doc-1",
                        "documentSha256": SHA256,
                        "section": "requirements",
                        "text": JD_TEXT,
                        "charStart": 0,
                        "charEnd": len(JD_TEXT),
                    }
                ],
                "requirements": [
                    _unresolved(f"req-{index}", label) for index, label in enumerate(labels)
                ],
            },
            "matchingPolicy": {
                "policyVersion": "balanced-v1",
                "bm25ProviderMode": "in_memory",
                "semanticAggregation": aggregation,
            },
        }
    )


def semantic_of(result):
    return next(item for item in result.factor_results if item.factor == "semantic")


class TestTheDefaultIsUnchanged:
    def test_the_document_policy_still_scores(self):
        result = MatchingFacade(StubEmbedder()).match(_request("Java backend service"))
        assert semantic_of(result).status in {"scored", "unknown"}

    def test_the_default_policy_is_document(self):
        request = _request("Java backend service")
        assert request.matching_policy.semantic_aggregation == "document"


class TestCoveragePolicy:
    def test_an_answered_requirement_scores(self):
        result = MatchingFacade(StubEmbedder()).match(
            _request("Java backend service", aggregation="requirement_coverage")
        )
        factor = semantic_of(result)
        assert factor.status == "scored" and factor.raw_score > 0.5

    def test_an_unanswered_requirement_scores_low(self):
        result = MatchingFacade(StubEmbedder()).match(
            _request("piloting a commercial airliner", aggregation="requirement_coverage")
        )
        assert semantic_of(result).raw_score < 0.2

    def test_adding_an_unmeetable_requirement_never_raises_the_score(self):
        """MR2, at the level the score is computed."""
        before = MatchingFacade(StubEmbedder()).match(
            _request("Java backend service", aggregation="requirement_coverage")
        )
        after = MatchingFacade(StubEmbedder()).match(
            _request(
                "Java backend service",
                "piloting a commercial airliner",
                aggregation="requirement_coverage",
            )
        )
        assert semantic_of(after).raw_score <= semantic_of(before).raw_score + 1e-9

    def test_repeating_a_passage_does_not_raise_the_score(self):
        """MR3, at the level the score is computed."""
        single = MatchingFacade(StubEmbedder()).match(
            _request("Java backend service", aggregation="requirement_coverage", spans=1)
        )
        repeated_request = _request(
            "Java backend service", aggregation="requirement_coverage", spans=1
        )
        # A verbatim copy of the one passage, which the CV would carry twice.
        original = repeated_request.resume.evidence[0]
        copied = original.model_copy(update={"evidenceId": "cv-ev-copy"})
        doubled = repeated_request.model_copy(
            update={
                "resume": repeated_request.resume.model_copy(
                    update={"evidence": [original, copied]}
                )
            }
        )
        after = MatchingFacade(StubEmbedder()).match(doubled)
        assert semantic_of(after).raw_score == pytest.approx(
            semantic_of(single).raw_score, abs=1e-6
        )

    def test_it_cites_the_passages_the_assignment_kept(self):
        result = MatchingFacade(StubEmbedder()).match(
            _request("Java backend service", aggregation="requirement_coverage")
        )
        assert semantic_of(result).evidence_refs == ["cv-ev-0"]
