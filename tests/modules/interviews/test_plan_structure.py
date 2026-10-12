from src.modules.interviews.planning.plan_structure import (
    build_evaluation_targets,
    build_sections,
    derive_difficulty,
    validate_must_have_coverage,
)
from src.modules.matching.domain.schemas import (
    CanonicalJob,
    LanguageRequirement,
    MatchResult,
    RequirementResult,
    SkillRequirement,
)
from src.modules.user_cvs.schemas import EvidenceSpan, TaxonomyRef


def _evidence(evidence_id: str, offset: int) -> EvidenceSpan:
    return EvidenceSpan(
        evidenceId=evidence_id,
        documentId="jd-doc",
        documentSha256="a" * 64,
        section="requirements",
        text=evidence_id,
        charStart=offset,
        charEnd=offset + len(evidence_id),
    )


def _job(*requirements, seniority=None) -> CanonicalJob:
    return CanonicalJob(
        schemaVersion="2.1",
        jobId="job-1",
        documentId="jd-doc",
        documentSha256="a" * 64,
        seniority=seniority,
        requirements=list(requirements),
        evidence=[
            _evidence(requirement.source_evidence_ref, index * 30)
            for index, requirement in enumerate(requirements)
        ],
    )


def _match(*results) -> MatchResult:
    return MatchResult(
        resumeId="cv-1",
        jobId="job-1",
        policyVersion="balanced-v1",
        eligibility="review_required",
        fitBand="review_required",
        decision="abstained",
        requirementResults=list(results),
        factorResults=[],
    )


def _result(requirement_id: str, status: str, reason: str) -> RequirementResult:
    return RequirementResult(
        requirementId=requirement_id,
        status=status,
        score=1.0 if status == "met" else None,
        confidence=1.0 if status == "met" else 0.0,
        evidenceRefs=[f"cv-{requirement_id}"] if status in {"met", "unknown"} else [],
        reasonCode=reason,
    )


def test_sections_are_deterministic_and_preserve_exact_duration() -> None:
    first = build_sections(25)
    second = build_sections(25)

    assert first == second
    assert [item["sectionId"] for item in first] == [
        "warmup",
        "core",
        "gap_validation",
        "closing",
    ]
    assert sum(item["durationMinutes"] for item in first) == 25
    assert all(item["durationMinutes"] >= 1 for item in first)


def test_difficulty_uses_explicit_job_seniority_first() -> None:
    assert derive_difficulty(_job(seniority="fresher"))["level"] == "foundational"
    assert derive_difficulty(_job(seniority="mid"))["level"] == "intermediate"
    assert derive_difficulty(_job(seniority="senior"))["level"] == "advanced"
    # The admin-edited column wins over the parser.
    assert derive_difficulty(_job(seniority="senior"), listing={"seniority": "junior"}) == {
        "level": "foundational",
        "source": "job_listing_seniority",
        "seniority": "junior",
    }


def test_difficulty_falls_back_to_title_then_required_years() -> None:
    by_title = derive_difficulty(_job(), listing={"title": "Android Engineer I", "experience_min_years": 4})
    assert (by_title["level"], by_title["source"]) == ("foundational", "job_title_level")
    assert derive_difficulty(_job(), listing={"title": "Staff Software Engineer"})["level"] == "advanced"
    assert derive_difficulty(_job(), listing={"title": "Software Engineer II"})["level"] == "intermediate"

    for years, level in ((1, "foundational"), (2, "intermediate"), (4, "intermediate"), (6, "advanced")):
        derived = derive_difficulty(_job(), listing={"title": "Backend Software Engineer", "experience_min_years": years})
        assert (derived["level"], derived["source"]) == (level, "job_min_experience_years")


def test_difficulty_default_is_visible_not_silently_off() -> None:
    # "unspecified" switched difficulty off in the selector for 54 of 55 plans.
    assert derive_difficulty(_job()) == {
        "level": "intermediate",
        "source": "default_no_job_signal",
        "seniority": None,
    }


def test_evaluation_targets_cover_non_taxonomy_language_requirement() -> None:
    python = SkillRequirement(
        requirementId="req-python",
        priority="must_have",
        sourceEvidenceRef="jd-python",
        type="skill",
        skill=TaxonomyRef(
            conceptId="skill.python",
            scheme="skill",
            taxonomyVersion="career-v1",
            label="Python",
        ),
    )
    english = LanguageRequirement(
        requirementId="req-english",
        priority="must_have",
        sourceEvidenceRef="jd-english",
        type="language",
        languageCode="en",
    )
    job = _job(python, english)
    targets = build_evaluation_targets(
        job=job,
        match=_match(
            _result("req-python", "met", "skill_evidenced"),
            _result("req-english", "unknown", "language_level_not_evidenced"),
        ),
    )

    assert [item["requirementId"] for item in targets] == [
        "req-python",
        "req-english",
    ]
    language = targets[1]
    assert language["kind"] == "language"
    assert language["evaluationMode"] == "requirement_validation"
    assert language["attention"] == "validate_gap"
    assert language["jobEvidenceRefs"] == ["jd-english"]
    validate_must_have_coverage(job=job, evaluation_targets=targets)


def test_missing_match_result_stays_explicit_unknown_instead_of_disappearing() -> None:
    requirement = LanguageRequirement(
        requirementId="req-english",
        priority="must_have",
        sourceEvidenceRef="jd-english",
        type="language",
        languageCode="en",
    )
    targets = build_evaluation_targets(job=_job(requirement), match=_match())

    assert targets[0]["status"] == "unknown"
    assert targets[0]["reasonCode"] == "match_result_missing"
    assert targets[0]["attention"] == "validate_gap"
