"""A posting's demand about sex, age or appearance gets no verdict at all.

Gating already kept these out of eligibility and out of the score. What it did
not stop was the evaluators answering them anyway: on the frozen corpus "Nam
từ 25 đến 35 tuổi" came back ``met``, with the candidate's degree line cited
as the evidence for it.
"""

import pytest

from src.modules.matching.application.facade import MatchingFacade, _requirement_gate
from src.modules.matching.domain.schemas import MatchRequest, UnresolvedRequirement

SHA256 = "b" * 64


class StubEmbedder:
    def score(self, *_args, **_kwargs):
        return None

    def embed(self, *_args, **_kwargs):
        return None


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


CV_TEXT = "Cử nhân ngành Hệ thống Thông tin, Trường Đại học Công nghệ Thông tin"
JD_TEXT = "Yêu cầu"


def _request(*labels: str) -> MatchRequest:
    return MatchRequest.model_validate(
        {
            "schemaVersion": "2.1",
            "resume": {
                "schemaVersion": "2.1",
                "resumeId": "cv-1",
                "documentId": "cv-doc-1",
                "documentSha256": SHA256,
                "rawText": CV_TEXT,
                "skills": [],
                "evidence": [
                    {
                        "evidenceId": "cv-ev-1",
                        "documentId": "cv-doc-1",
                        "documentSha256": SHA256,
                        "section": "education",
                        "text": CV_TEXT,
                        "charStart": 0,
                        "charEnd": len(CV_TEXT),
                    }
                ],
            },
            "job": {
                "schemaVersion": "2.1",
                "jobId": "job-1",
                "documentId": "job-doc-1",
                "documentSha256": SHA256,
                "jobTitle": "Lập trình viên",
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
                    _unresolved(f"req-{index}", label)
                    for index, label in enumerate(labels)
                ],
            },
            "matchingPolicy": {"policyVersion": "balanced-v1", "bm25ProviderMode": "in_memory"},
        }
    )


class TestGateClassification:
    @pytest.mark.parametrize(
        "label",
        [
            "Nam từ 25 đến 35 tuổi",
            "Nữ, ngoại hình ưa nhìn",
            "Có giọng nói truyền cảm",
        ],
    )
    def test_protected_and_physical_lines_are_not_assessable(self, label):
        requirement = UnresolvedRequirement.model_validate(_unresolved("req-0", label))
        assert _requirement_gate(requirement).gate_class == "non_assessable"

    @pytest.mark.parametrize(
        "label",
        [
            "Hiểu biết tốt về Design pattern, OOP và Web API",
            "Tốt nghiệp Đại học chuyên ngành Công nghệ Thông tin",
        ],
    )
    def test_ordinary_requirements_are_untouched(self, label):
        requirement = UnresolvedRequirement.model_validate(_unresolved("req-0", label))
        assert _requirement_gate(requirement).gate_class == "objective"


class TestNoVerdictIsReported:
    def test_a_sex_and_age_line_is_reported_as_not_applicable(self):
        result = MatchingFacade(StubEmbedder()).match(_request("Nam từ 25 đến 35 tuổi"))
        (requirement,) = result.requirement_results
        assert requirement.status == "not_applicable"

    def test_it_is_never_reported_as_met(self):
        """The defect measured on the frozen corpus: a degree line answered it."""
        result = MatchingFacade(StubEmbedder()).match(_request("Nam từ 25 đến 35 tuổi"))
        assert result.requirement_results[0].status != "met"

    def test_it_is_not_reported_as_unmet_either(self):
        """"Not met" reads as the candidate falling short of it. They do not."""
        result = MatchingFacade(StubEmbedder()).match(_request("Nữ, ngoại hình ưa nhìn"))
        assert result.requirement_results[0].status != "not_met"

    def test_it_cites_no_evidence_from_the_candidate(self):
        result = MatchingFacade(StubEmbedder()).match(_request("Nam từ 25 đến 35 tuổi"))
        assert result.requirement_results[0].evidence_refs == []

    def test_it_carries_the_reason_it_was_set_aside(self):
        result = MatchingFacade(StubEmbedder()).match(_request("Nam từ 25 đến 35 tuổi"))
        assert result.requirement_results[0].reason_code == "protected_or_physical_attribute"

    def test_it_never_disqualifies_the_candidate(self):
        result = MatchingFacade(StubEmbedder()).match(_request("Nam từ 25 đến 35 tuổi"))
        assert result.failed_must_have_requirements == []
        assert result.eligibility != "ineligible"

    def test_an_ordinary_requirement_beside_it_is_still_evaluated(self):
        result = MatchingFacade(StubEmbedder()).match(
            _request("Nam từ 25 đến 35 tuổi", "Hiểu biết tốt về Design pattern và OOP")
        )
        statuses = {item.requirement_id: item.status for item in result.requirement_results}
        assert statuses["req-0"] == "not_applicable"
        assert statuses["req-1"] != "not_applicable"
