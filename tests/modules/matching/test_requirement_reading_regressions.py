"""How a posting's sentence is read, measured on one real CV/JD pair.

Every case here was produced by a real candidate against a real posting
("Thực tập sinh IT"): a Java/JavaScript developer who graduated in 2017 and
whose CV names HTML5 and CSS3 was reported as failing six of eight must-haves,
including every one of these.  The failures were in the reading, not in the CV.
"""

from datetime import UTC, datetime

import pytest

from src.modules.job_descriptions.schemas import requirement_list_is_disjunctive
from src.modules.matching.domain.schemas import UnresolvedRequirement
from src.modules.matching.evaluation.requirement_evaluators import (
    evaluate_unresolved_requirement,
    select_evaluator,
)
from src.modules.taxonomy.facade import alias_pattern
from src.modules.user_cvs.schemas import CanonicalResume

_TAXONOMY = "internal-career-2026.1"


def _evidence(evidence_id: str, section: str, text: str, start: int = 0) -> dict[str, object]:
    return {
        "evidenceId": evidence_id,
        "documentId": "cv-1",
        "documentSha256": "a" * 64,
        "section": section,
        "text": text,
        "charStart": start,
        "charEnd": start + len(text),
    }


def _skill(concept_id: str, label: str, refs: list[str]) -> dict[str, object]:
    return {
        "claimId": f"claim-{concept_id}",
        "concept": {
            "conceptId": concept_id,
            "scheme": "internal",
            "taxonomyVersion": _TAXONOMY,
            "label": label,
        },
        "rawLabel": label,
        "evidenceRefs": refs,
    }


def _resume(
    *,
    evidence: list[dict[str, object]],
    skills: list[dict[str, object]] | None = None,
    education: list[dict[str, object]] | None = None,
) -> CanonicalResume:
    raw_text = "\n".join(str(item["text"]) for item in evidence)
    return CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "cv-1",
            "documentId": "cv-1",
            "documentSha256": "a" * 64,
            "parsing": {
                "parserVersion": "test",
                "extractionVersion": "test",
                "parsedAt": datetime.now(UTC).isoformat(),
                "status": "ready",
                "rawTextCoverage": "complete",
                "evidenceIndexCoverage": "complete",
            },
            "evidence": evidence,
            "skills": skills or [],
            "education": education or [],
        }
    ).model_copy(update={"raw_text": raw_text})


def _requirement(
    label: str,
    *,
    concepts: list[tuple[str, str]] | None = None,
    group_operator: str | None = None,
    kind: str = "skill",
) -> UnresolvedRequirement:
    payload: dict[str, object] = {
        "requirementId": "req-1",
        "type": "unresolved",
        "kind": kind,
        "priority": "must_have",
        "sourceEvidenceRef": "jd-1",
        "rawLabel": label,
    }
    if concepts:
        payload["atomicConcepts"] = [
            {
                "conceptId": concept_id,
                "scheme": "internal",
                "taxonomyVersion": _TAXONOMY,
                "label": label_,
            }
            for concept_id, label_ in concepts
        ]
        payload["groupOperator"] = group_operator or "all_of"
    return UnresolvedRequirement.model_validate(payload)


# ---------------------------------------------------------------- disjunction


@pytest.mark.parametrize(
    "label",
    [
        "Thành thạo ít nhất một ngôn ngữ lập trình (C, C++, Java, C#, JavaScript,...).",
        "Proficient in at least one programming language (C, C++, Java).",
        "Biết một trong các ngôn ngữ: Java, Python, Go",
        "Thành thạo các ngôn ngữ như Java, Python, C#",
        "Familiar with a backend language such as Java, Go or Python",
        "Sử dụng được Java, Python, C#, v.v",
    ],
)
def test_a_list_the_posting_opens_or_illustrates_offers_alternatives(label: str) -> None:
    assert requirement_list_is_disjunctive(label) is True


@pytest.mark.parametrize(
    "label",
    [
        "Thành thạo Java, Spring Boot và MySQL",
        "Strong experience with React, TypeScript and Node.js",
        # "nhu cầu" folds to "nhu cau"; the example marker must not eat it.
        "Nắm bắt nhu cầu của khách hàng và triển khai Java, Spring Boot",
        "Thực hiện đúng như trên với Docker và Kubernetes",
    ],
)
def test_a_plain_conjunction_stays_a_conjunction(label: str) -> None:
    assert requirement_list_is_disjunctive(label) is False


def test_at_least_one_language_is_met_by_evidencing_one_of_them() -> None:
    """The posting listed five languages and asked for one; it was read as all five.

    Stored as ``all_of`` by the JD parser, the requirement could only be met by
    a CV naming C, C++, Java, C# and JavaScript together - a must-have no
    candidate can satisfy, failed here by a Java and JavaScript developer.
    """
    resume = _resume(
        evidence=[
            _evidence("ev-1", "summary", "Built backend services with Java and Spring Boot.", 0),
            _evidence("ev-2", "projects", "Developed a React frontend in JavaScript.", 60),
        ],
        skills=[
            _skill("skill-java", "Java", ["ev-1"]),
            _skill("skill-javascript", "JavaScript", ["ev-2"]),
        ],
    )
    requirement = _requirement(
        "Thành thạo ít nhất một ngôn ngữ lập trình (C, C++, Java, C#, JavaScript,...).",
        concepts=[
            ("skill-c", "C"),
            ("skill-cpp", "C++"),
            ("skill-java", "Java"),
            ("skill-csharp", "C#"),
            ("skill-javascript", "JavaScript"),
        ],
        group_operator="all_of",
    )

    result = evaluate_unresolved_requirement(requirement, resume)

    assert result.group_operator == "any_of"
    assert result.status == "met"
    assert result.reason_code == "concept_group_evidenced"


def test_a_conjunction_is_not_waived_by_the_override() -> None:
    """Only ``all_of`` is overridden, and only when the sentence says so."""
    resume = _resume(
        evidence=[_evidence("ev-1", "summary", "Built backend services with Java.", 0)],
        skills=[_skill("skill-java", "Java", ["ev-1"])],
    )
    requirement = _requirement(
        "Thành thạo Java và C#",
        concepts=[("skill-java", "Java"), ("skill-csharp", "C#")],
        group_operator="all_of",
    )

    result = evaluate_unresolved_requirement(requirement, resume)

    assert result.group_operator == "all_of"
    assert result.status == "not_met"


# ------------------------------------------------------------ version suffixes


@pytest.mark.parametrize(
    ("alias", "text"),
    [
        ("html", "JavaScript (ES6+), React, HTML5, CSS3, and Material UI"),
        ("css", "JavaScript (ES6+), React, HTML5, CSS3, and Material UI"),
        ("vue", "Built the admin panel in Vue 3"),
        ("java", "Java 17, Spring Boot 3"),
        ("php", "PHP8 and Laravel"),
    ],
)
def test_a_technology_is_recognised_with_its_version_in_the_name(alias: str, text: str) -> None:
    assert alias_pattern(alias).search(text) is not None


@pytest.mark.parametrize(
    ("alias", "text"),
    [
        # The suffix is digits, so a longer name is still a different technology.
        ("java", "JavaScript developer"),
        ("html", "htmlx templates"),
        ("css", "cssnano"),
        # Short and punctuated aliases keep their exact boundaries.
        ("js", "node.js"),
    ],
)
def test_the_version_suffix_does_not_widen_a_name_into_another(alias: str, text: str) -> None:
    assert alias_pattern(alias).search(text) is None


def test_html_and_css_written_with_versions_evidence_the_web_requirement() -> None:
    """"Lập trình Web (HTML/CSS)" found nothing in a CV that names HTML5 and CSS3."""
    resume = _resume(
        evidence=[
            _evidence(
                "ev-1",
                "projects",
                "I have developed a foundation in JavaScript (ES6+), React, HTML5, CSS3, "
                "and Material UI to create web applications.",
                0,
            )
        ]
    )
    requirement = _requirement(
        "Có kiến thức cơ bản về lập trình Web (HTML/CSS) và cơ sở dữ liệu.",
        concepts=[("skill-html", "HTML"), ("skill-css", "CSS")],
        group_operator="all_of",
    )

    result = evaluate_unresolved_requirement(requirement, resume)

    assert result.status == "met"
    assert result.evidence_refs == ["ev-1"]


def test_a_concept_is_searched_for_under_the_catalogues_own_wordings() -> None:
    """The catalogue lists "algorithms"; the local alias table listed nothing."""
    resume = _resume(
        evidence=[
            _evidence(
                "ev-1",
                "skills",
                "BIG-O CODING: BIG-O GREEN: Elementary algorithms, BIG-O BLUE: "
                "intermediate algorithms",
                0,
            )
        ]
    )
    requirement = _requirement(
        "Cấu trúc dữ liệu và giải thuật",
        concepts=[("skill-data-structures-algorithms", "Data Structures and Algorithms")],
    )

    result = evaluate_unresolved_requirement(requirement, resume)

    assert result.evidence_refs == ["ev-1"]
    assert result.status != "not_met"


# ------------------------------------------------------------ student status


def _graduate_resume(field: str) -> CanonicalResume:
    return _resume(
        evidence=[
            _evidence(
                "ev-edu",
                "education",
                f"Ho Chi Minh City University of Technology: {field}, 10/2012 - 3/2017",
                0,
            )
        ],
        education=[
            {
                "educationId": "edu-1",
                "institution": "Ho Chi Minh City University of Technology",
                "fieldOfStudy": field,
                "startDate": {"value": "2012-10", "precision": "month"},
                "endDate": {"value": "2017-03", "precision": "month"},
                "evidenceRefs": ["ev-edu"],
            },
            # A later short course with no end date must not be mistaken for a
            # degree that might still be running.
            {
                "educationId": "edu-2",
                "institution": "VTI Academy",
                "startDate": {"value": "2025", "precision": "year"},
                "evidenceRefs": [],
            },
        ],
    )


def _student_requirement() -> UnresolvedRequirement:
    return _requirement(
        "Sinh viên năm cuối chuyên ngành Công nghệ Thông tin tại các trường đại học",
        kind="education",
    )


def test_final_year_student_in_vietnamese_reaches_the_education_evaluator() -> None:
    """"Sinh viên năm cuối" was missing, so the line fell through to word overlap."""
    selection = select_evaluator(_student_requirement())

    assert selection is not None
    assert selection.name == "education_status"


def test_finishing_the_degree_exceeds_a_final_year_requirement() -> None:
    """"Năm cuối" is a floor.  Holding the degree is past it, not short of it.

    Read as an exact point, the line rejected a candidate for having finished
    what it asks them to be close to finishing - the same error as reading
    "Đại học trở lên" to exclude a master's.
    """
    result = evaluate_unresolved_requirement(
        _student_requirement(), _graduate_resume("Information Technology")
    )

    assert result.status == "met"
    assert result.reason_code == "education_status_exceeded"
    assert result.evidence_refs == ["ev-edu"]


@pytest.mark.parametrize("field", ["Khoa học máy tính", "Kỹ thuật phần mềm"])
def test_a_vietnamese_major_answers_a_vietnamese_posting(field: str) -> None:
    """The field vocabulary held five English-leaning phrases and nothing else."""
    result = evaluate_unresolved_requirement(_student_requirement(), _graduate_resume(field))

    assert result.status == "met"


def test_a_degree_in_another_field_is_asked_about_rather_than_rejected() -> None:
    """The named-field vocabulary is narrower than the majors that qualify.

    ``unknown`` keeps the pair out of the eligibility gate and raises the
    question; ``not_met`` would disqualify on a word list.
    """
    result = evaluate_unresolved_requirement(
        _student_requirement(), _graduate_resume("Automation Engineer")
    )

    assert result.status == "unknown"
    assert result.reason_code == "education_field_not_confirmed"
    assert result.evidence_refs == ["ev-edu"]
    assert "Automation Engineer" in (result.evidence_explanation or "")


def test_a_cv_with_no_degree_at_all_still_fails_the_requirement() -> None:
    resume = _resume(
        evidence=[_evidence("ev-1", "summary", "Self-taught developer, five years of PHP.", 0)]
    )

    result = evaluate_unresolved_requirement(_student_requirement(), resume)

    assert result.status == "not_met"
    assert result.reason_code == "education_status_not_evidenced"


def test_a_degree_still_running_is_never_concluded_against() -> None:
    this_year = datetime.now(UTC).year
    resume = _resume(
        evidence=[
            _evidence(
                "ev-edu",
                "education",
                f"University of Information Technology, Computer Science, {this_year - 3} - {this_year + 1}",
                0,
            )
        ],
        education=[
            {
                "educationId": "edu-1",
                "institution": "University of Information Technology",
                "fieldOfStudy": "Computer Science",
                "startDate": {"value": f"{this_year - 3}", "precision": "year"},
                "endDate": {"value": f"{this_year + 1}", "precision": "year"},
                "evidenceRefs": ["ev-edu"],
            }
        ],
    )

    result = evaluate_unresolved_requirement(_student_requirement(), resume)

    assert result.status == "met"
    assert result.reason_code == "education_status_and_field_evidenced"
