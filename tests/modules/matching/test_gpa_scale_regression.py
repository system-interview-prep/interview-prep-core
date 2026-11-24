"""A cumulative-grade threshold written in Vietnamese, against a 4.0-scale CV.

A posting that asks for "Điểm trung bình tích lũy từ 7.0/10 trở lên" is stating
the same condition as one that writes "GPA >= 7.0/10", and a CV reading
"GPA: 3.23 / 4.0" clears it - 3.23/4.0 is 0.81, the requirement asks for 0.70.
The system reported "không tìm thấy" instead, because nothing on either side of
the pipeline recognised the Vietnamese phrasing: the JD parser left the
requirement with no threshold and no scale, and the matcher's own detector read
only the token "GPA", so the line went to the skill evaluator, which looked for
a technology named "điểm trung bình tích lũy" among the candidate's skills.
"""

from __future__ import annotations

import pytest

from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser
from src.modules.matching.domain.schemas import UnresolvedRequirement
from src.modules.matching.evaluation.requirement_evaluators import (
    evaluate_unresolved_requirement,
    select_evaluator,
)
from src.modules.user_cvs.facade import SourceDocument
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.source import build_source_document
from src.modules.user_cvs.schemas import CanonicalResume

SHA = "c" * 64
CV_EDUCATION_LINE = (
    "HCMC University of Technology (HUTECH) 2022 - 2026 "
    "Major: Software Engineering GPA: 3.23 / 4.0"
)


def _make_source(text: str) -> SourceDocument:
    lines = text.splitlines()
    return build_source_document(
        DocumentArtifacts(
            markdown=text,
            content_list=[{"type": "text", "text": line, "page_idx": 0} for line in lines],
        ),
        document_id="jd-gpa-test",
        document_sha256="a" * 64,
    )


def _requirement(raw_label: str, **overrides) -> UnresolvedRequirement:
    return UnresolvedRequirement.model_validate(
        {
            "requirementId": "req-gpa",
            "type": "unresolved",
            "sourceEvidenceRef": "jd-ev-1",
            "kind": "education",
            "priority": "must_have",
            "rawLabel": raw_label,
            **overrides,
        }
    )


def _resume(gpa: float, scale: float, text: str = CV_EDUCATION_LINE) -> CanonicalResume:
    return CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "resume-gpa-fixture",
            "documentId": "resume-gpa-fixture",
            "documentSha256": SHA,
            "education": [
                {
                    "educationId": "education-hutech",
                    "institution": "HCMC University of Technology (HUTECH)",
                    "degree": "Software Engineering",
                    "fieldOfStudy": "Software Engineering",
                    "gpa": gpa,
                    "gpaScale": scale,
                    "evidenceRefs": ["cv-education"],
                }
            ],
            "evidence": [
                {
                    "evidenceId": "cv-education",
                    "documentId": "resume-gpa-fixture",
                    "documentSha256": SHA,
                    "section": "education",
                    "text": text,
                    "charStart": 0,
                    "charEnd": len(text),
                    "page": 1,
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("line", "threshold", "scale"),
    [
        ("- Điểm trung bình tích lũy từ 7.0/10 trở lên", 7.0, 10.0),
        ("- Điểm trung bình tích lũy đạt 3.2/4.0 trở lên", 3.2, 4.0),
        ("- ĐTB >= 3.2/4", 3.2, 4.0),
        # No explicit scale: the threshold's own size still decides it.
        ("- Điểm trung bình tích lũy từ 7.0 trở lên", 7.0, 10.0),
        # The English wording the parser already handled must keep working.
        ("- GPA >= 3.2/4.0", 3.2, 4.0),
    ],
)
def test_jd_parser_reads_a_vietnamese_grade_threshold(line, threshold, scale):
    jd_text = "Yêu cầu công việc:\n" + line + "\n"
    canonical = DeterministicJobDescriptionParser().parse(
        _make_source(jd_text), extraction_version="test-gpa"
    )

    graded = [item for item in canonical.requirements if item.threshold == threshold]
    assert len(graded) == 1, [item.raw_label for item in canonical.requirements]
    assert graded[0].kind == "education"
    assert graded[0].scale == scale


@pytest.mark.parametrize(
    "line",
    [
        # A count of subjects is not a grade, and has no scale to anchor it.
        "- Điểm trung bình 3 môn chuyên ngành",
        "- Thành thạo ít nhất một ngôn ngữ lập trình (C, C++, Java, C#, JavaScript)",
        "- Có 3 năm kinh nghiệm Java",
    ],
)
def test_jd_parser_does_not_invent_a_grade_threshold(line):
    canonical = DeterministicJobDescriptionParser().parse(
        _make_source("Yêu cầu công việc:\n" + line + "\n"), extraction_version="test-gpa"
    )
    assert all(item.scale is None for item in canonical.requirements)


def test_vietnamese_grade_requirement_reaches_the_gpa_evaluator():
    requirement = _requirement("Điểm trung bình tích lũy từ 7.0/10 trở lên")
    assert select_evaluator(requirement).name == "gpa"


@pytest.mark.parametrize(
    ("threshold", "scale"),
    [
        # The parser supplies both now; a cached or LLM parse may supply neither,
        # and the requirement is no less a grade threshold then.
        (None, None),
        (7.0, 10.0),
    ],
)
def test_four_point_gpa_clears_a_ten_point_requirement(threshold, scale):
    requirement = _requirement(
        "Điểm trung bình tích lũy từ 7.0/10 trở lên", threshold=threshold, scale=scale
    )
    result = evaluate_unresolved_requirement(requirement, _resume(3.23, 4.0))

    assert result.status == "met"
    assert result.reason_code == "gpa_threshold_met"
    assert result.evidence_refs == ["cv-education"]


def test_four_point_gpa_below_the_requirement_still_fails():
    requirement = _requirement("Điểm trung bình tích lũy từ 7.0/10 trở lên")
    result = evaluate_unresolved_requirement(requirement, _resume(2.4, 4.0))

    assert result.status == "not_met"
    assert result.reason_code == "gpa_below_threshold"


def test_scale_is_read_from_the_cv_rather_than_guessed():
    """The slash in "3.23 / 4.0" has to survive normalization.

    Dropping it left two bare numbers, the scale was guessed from the value's
    size, and a 4.0-scale grade was then measured against a 10-point rule.
    """

    requirement = _requirement("Điểm trung bình tích lũy từ 7.0/10 trở lên")
    # No structured education entry, so the evaluator must parse the CV line.
    resume = CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "resume-gpa-text-only",
            "documentId": "resume-gpa-text-only",
            "documentSha256": SHA,
            "evidence": [
                {
                    "evidenceId": "cv-education",
                    "documentId": "resume-gpa-text-only",
                    "documentSha256": SHA,
                    "section": "education",
                    "text": CV_EDUCATION_LINE,
                    "charStart": 0,
                    "charEnd": len(CV_EDUCATION_LINE),
                    "page": 1,
                }
            ],
        }
    )
    result = evaluate_unresolved_requirement(requirement, resume)

    assert result.status == "met"
    assert result.reason_code == "gpa_threshold_met"
