from src.modules.interviews.planner import (
    PLANNER_POLICY_VERSION,
    derive_competency_plan,
)
from src.modules.matching.schemas import (
    CanonicalJob,
    ConceptResult,
    MatchResult,
    RequirementResult,
    SkillRequirement,
    UnresolvedRequirement,
)
from src.modules.user_cvs.schemas import CareerClassification, EvidenceSpan, TaxonomyRef


def _concept(concept_id: str, label: str) -> TaxonomyRef:
    return TaxonomyRef(
        conceptId=concept_id,
        scheme="skill",
        taxonomyVersion="career-v1",
        label=label,
    )


def _job(*requirements, classifications=None, seniority=None) -> CanonicalJob:
    evidence = []
    for index, requirement in enumerate(requirements, start=1):
        text_value = f"requirement-{index}"
        evidence.append(
            EvidenceSpan(
                evidenceId=requirement.source_evidence_ref,
                documentId="jd-doc",
                documentSha256="a" * 64,
                section="requirements",
                text=text_value,
                charStart=(index - 1) * 20,
                charEnd=(index - 1) * 20 + len(text_value),
            )
        )
    return CanonicalJob(
        schemaVersion="2.1",
        jobId="job-1",
        documentId="jd-doc",
        documentSha256="a" * 64,
        requirements=list(requirements),
        careerClassifications=classifications or [],
        seniority=seniority,
        evidence=evidence,
    )


def _match(results) -> MatchResult:
    return MatchResult(
        resumeId="cv-1",
        jobId="job-1",
        policyVersion="balanced-v1",
        eligibility="review_required",
        suitabilityScore=None,
        fitBand="review_required",
        decision="abstained",
        requirementResults=results,
        factorResults=[],
        warnings=[],
    )


def _result(requirement_id: str, status: str) -> RequirementResult:
    return RequirementResult(
        requirementId=requirement_id,
        status=status,
        score=1.0 if status == "met" else None,
        confidence=1.0 if status == "met" else 0.0,
        evidenceRefs=[],
        reasonCode=f"test_{status}",
    )


def test_planner_prioritizes_unresolved_must_have_without_skipping_met_claims() -> None:
    python = SkillRequirement(
        requirementId="req-python",
        priority="must_have",
        sourceEvidenceRef="jd-ev-python",
        type="skill",
        skill=_concept("skill.python", "Python"),
    )
    docker = SkillRequirement(
        requirementId="req-docker",
        priority="must_have",
        sourceEvidenceRef="jd-ev-docker",
        type="skill",
        skill=_concept("skill.docker", "Docker"),
    )
    plan = derive_competency_plan(
        job=_job(python, docker),
        match=_match([
            _result("req-python", "met"),
            _result("req-docker", "unknown"),
        ]),
        duration_minutes=20,
    )

    assert plan["policyVersion"] == PLANNER_POLICY_VERSION
    assert plan["targets"][0]["conceptId"] == "skill.docker"
    assert {item["conceptId"] for item in plan["targets"]} == {
        "skill.python",
        "skill.docker",
    }
    assert plan["targets"][0]["rationale"]["matchStatuses"] == ["unknown"]


def test_planner_preserves_atomic_concepts_and_allocates_bounded_budget() -> None:
    ai_group = UnresolvedRequirement(
        requirementId="req-ai",
        priority="must_have",
        sourceEvidenceRef="jd-ev-ai",
        type="unresolved",
        kind="skill",
        rawLabel="NLP, GenAI and LLM",
        atomicConcepts=[
            _concept("skill.nlp", "NLP"),
            _concept("skill.genai", "GenAI"),
            _concept("skill.llm", "LLM"),
        ],
        groupOperator="all_of",
    )
    atomic_result = RequirementResult(
        requirementId="req-ai",
        status="unknown",
        score=None,
        confidence=0.4,
        evidenceRefs=[],
        reasonCode="atomic_group_incomplete",
        groupOperator="all_of",
        conceptResults=[
            ConceptResult(
                conceptId="skill.nlp",
                label="NLP",
                status="met",
                confidence=1.0,
                evidenceRefs=[],
                reasonCode="concept_evidenced",
            ),
            ConceptResult(
                conceptId="skill.genai",
                label="GenAI",
                status="met",
                confidence=1.0,
                evidenceRefs=[],
                reasonCode="concept_evidenced",
            ),
            ConceptResult(
                conceptId="skill.llm",
                label="LLM",
                status="unknown",
                confidence=0.0,
                evidenceRefs=[],
                reasonCode="concept_evidence_missing",
            ),
        ],
    )
    plan = derive_competency_plan(
        job=_job(ai_group),
        match=_match([atomic_result]),
        duration_minutes=25,
    )

    assert plan["questionBudget"] == 6
    assert plan["targetQuestionCount"] == 6
    assert {item["conceptId"] for item in plan["targets"]} == {
        "skill.nlp",
        "skill.genai",
        "skill.llm",
    }
    assert plan["targets"][0]["conceptId"] == "skill.llm"
    assert plan["targets"][0]["rationale"]["matchStatuses"] == ["unknown"]
    assert all(item["targetQuestionCount"] <= 3 for item in plan["targets"])


def test_planner_is_deterministic_for_same_job_match_and_duration() -> None:
    requirement = SkillRequirement(
        requirementId="req-python",
        priority="must_have",
        sourceEvidenceRef="jd-ev-python",
        type="skill",
        skill=_concept("skill.python", "Python"),
    )
    job = _job(requirement)
    match = _match([_result("req-python", "met")])

    first = derive_competency_plan(job=job, match=match, duration_minutes=25)
    second = derive_competency_plan(job=job, match=match, duration_minutes=25)

    assert first == second


def test_planner_uses_career_classification_only_as_explicit_fallback() -> None:
    job = _job(
        classifications=[
            CareerClassification(
                code="technology.artificial-intelligence",
                label="Artificial Intelligence",
                dimension="specialization",
                taxonomyVersion="career-v1",
                isPrimary=True,
                confidence=0.95,
                assertionSource="inferred",
                evidenceRefs=[],
            )
        ]
    )
    plan = derive_competency_plan(
        job=job,
        match=_match([]),
        duration_minutes=15,
    )

    assert plan["targets"][0]["conceptId"] == "technology.artificial-intelligence"
    assert plan["targets"][0]["rationale"]["source"] == "career_classification_fallback"


def test_planner_fails_closed_without_taxonomy_backed_targets() -> None:
    try:
        derive_competency_plan(
            job=_job(),
            match=_match([]),
            duration_minutes=15,
        )
    except ValueError as exc:
        assert "No taxonomy-backed interview competencies" in str(exc)
        return
    raise AssertionError("planner must fail closed when no competency can be derived")


def test_planner_preserves_non_skill_requirement_validation_and_structure() -> None:
    python = SkillRequirement(
        requirementId="req-python",
        priority="must_have",
        sourceEvidenceRef="jd-ev-python",
        type="skill",
        skill=_concept("skill.python", "Python"),
    )
    english = UnresolvedRequirement(
        requirementId="req-english",
        priority="must_have",
        sourceEvidenceRef="jd-ev-english",
        type="unresolved",
        kind="language",
        rawLabel="IELTS 6.0 or equivalent",
        credential="IELTS",
        equivalentAllowed=True,
    )
    plan = derive_competency_plan(
        job=_job(python, english, seniority="senior"),
        match=_match([
            _result("req-python", "met"),
            _result("req-english", "unknown"),
        ]),
        duration_minutes=20,
    )

    by_requirement = {
        item["requirementId"]: item for item in plan["evaluationTargets"]
    }
    assert by_requirement["req-python"]["evaluationMode"] == "competency"
    assert by_requirement["req-english"]["evaluationMode"] == "requirement_validation"
    assert by_requirement["req-english"]["attention"] == "validate_gap"
    assert "req-english" in plan["skippedRequirementIds"]
    assert plan["difficulty"] == {
        "level": "advanced",
        "source": "job_seniority",
        "seniority": "senior",
    }
    assert sum(item["durationMinutes"] for item in plan["sections"]) == 20
    assert [item["sectionId"] for item in plan["sections"]] == [
        "warmup",
        "core",
        "gap_validation",
        "closing",
    ]
