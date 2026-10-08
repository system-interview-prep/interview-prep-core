"""Unit tests for P1 Planner Gate 2 Acceptance Cases and System Invariants.

Covers:
- TC-01 to TC-08 manual acceptance cases from INTERVIEW_P1_GATE2_SPEC_AND_ACCEPTANCE_CASES.md
- Strict Budget Non-Exceedance (Invariant 1)
- Exact Pool Conservation (Invariant 2)
- Zero Division Safety & Defensive Zero-Weight Fallback (Invariant 3)
- Two-Level Requirement/Competency Traceability (Invariant 4A & 4B)
- Modality & Question Bank Decoupling (Invariant 5)
- Floor Guarantee for Selected Targets (Invariant 6)
- Decisions 1, 5, 7 policy parameter injection
- All-not_applicable scenario (Option B)
- Legacy backward compatibility when policy_config is None
"""

from typing import Any
import pytest

from src.modules.interviews.planning import planner
from src.modules.interviews.planning.planner import (
    PLANNER_POLICY_VERSION,
    PLANNER_POLICY_VERSION_DYNAMIC,
    derive_competency_plan,
)
from src.modules.interviews.planning.planner_config import (
    PlannerPolicyConfig,
)
from src.modules.matching.domain.schemas import (
    CanonicalJob,
    ConceptResult,
    MatchResult,
    RequirementResult,
    SkillRequirement,
    UnresolvedRequirement,
)
from src.modules.user_cvs.schemas import EvidenceSpan, TaxonomyRef
from tests.modules.interviews.test_planner_policy_config import create_test_policy_config


def _concept(concept_id: str, label: str) -> TaxonomyRef:
    return TaxonomyRef(
        conceptId=concept_id,
        scheme="skill",
        taxonomyVersion="career-v1",
        label=label,
    )


def _job(*requirements, classifications=None, seniority=None, strict_hands_on_required: Any = None) -> CanonicalJob:
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
    job = CanonicalJob(
        schemaVersion="2.1",
        jobId="job-1",
        documentId="jd-doc",
        documentSha256="a" * 64,
        requirements=list(requirements),
        careerClassifications=classifications or [],
        seniority=seniority,
        evidence=evidence,
    )
    if strict_hands_on_required is not None or strict_hands_on_required is False:
        object.__setattr__(job, "strict_hands_on_required", strict_hands_on_required)
    return job


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


def _result(requirement_id: str, status: str, concept_results: list[ConceptResult] | None = None) -> RequirementResult:
    return RequirementResult(
        requirementId=requirement_id,
        status=status,
        score=1.0 if status == "met" else None,
        confidence=1.0 if status == "met" else 0.0,
        evidenceRefs=[],
        reasonCode=f"test_{status}",
        conceptResults=concept_results or [],
    )


# --- TC-01: Single TEXT Target ---


def test_tc01_single_text_target() -> None:
    """TC-01: Single TEXT target gets full tech pool with exact remainder."""
    sql = SkillRequirement(
        requirementId="req-sql",
        priority="must_have",
        sourceEvidenceRef="jd-ev-sql",
        type="skill",
        skill=_concept("concept-sql-basic", "SQL Query Basics"),
    )
    job = _job(sql)
    match = _match([_result("req-sql", "unknown")])

    # 15m session, tech_pool = 900 - (120 + 0 + 210 + 135 + 180) = 255s
    config = create_test_policy_config(
        t_onboarding_base=120,
        n_onboarding=1,
        t_cv_addon_inclusive=0,
        t_cv_standalone_reserve=0,
        t_behavioral=210,
        probe_pool_ratio=0.15,
        t_closing_reserve=180,
        coding_fallback_policy="downgrade_to_text",
        strict_priority_stop=True,
    )
    plan = derive_competency_plan(job=job, match=match, duration_minutes=15, policy_config=config)

    assert plan["policyVersion"] == PLANNER_POLICY_VERSION_DYNAMIC
    assert plan["techPoolSeconds"] == 255
    assert plan["unallocatedBufferSeconds"] == 0
    assert len(plan["targets"]) == 1
    t0 = plan["targets"][0]
    assert t0["conceptId"] == "concept-sql-basic"
    assert t0["targetArchetype"] == "TEXT"
    assert t0["timeEnvelopeSeconds"] == 255
    assert t0["floorSeconds"] == 180
    assert t0["estimatedQuestionsRange"] == [1, 2]
    assert len(plan["nonInterviewedTargets"]) == 0
    assert plan["estimatedTurnsRange"]["estimatedFrozenTurnsRange"] == [3, 4]

    # Invariants 1, 2, 6
    assert t0["timeEnvelopeSeconds"] <= plan["techPoolSeconds"]
    assert t0["timeEnvelopeSeconds"] + plan["unallocatedBufferSeconds"] == plan["techPoolSeconds"]
    assert t0["timeEnvelopeSeconds"] >= 180


# --- TC-02: Multiple Targets with Must-Have and Nice-To-Have ---


def test_tc02_multiple_targets_hamilton_hare_surplus_distribution() -> None:
    """TC-02: 3 targets distribute surplus via Hamilton-Hare with exact integer second sum."""
    c1 = SkillRequirement(
        requirementId="req-sys",
        priority="must_have",
        sourceEvidenceRef="jd-ev-sys",
        type="skill",
        skill=_concept("concept-system-arch", "System Architecture"),
    )
    c2 = SkillRequirement(
        requirementId="req-db",
        priority="must_have",
        sourceEvidenceRef="jd-ev-db",
        type="skill",
        skill=_concept("concept-database-opt", "Database Optimization"),
    )
    c3 = SkillRequirement(
        requirementId="req-redis",
        priority="nice_to_have",
        sourceEvidenceRef="jd-ev-redis",
        type="skill",
        skill=_concept("concept-redis-cache", "Redis Cache"),
    )
    job = _job(c1, c2, c3)
    match = _match([
        _result("req-sys", "unknown"),   # W = 1.0 * 1.2 = 1.20
        _result("req-db", "met"),        # W = 1.0 * 1.0 = 1.00
        _result("req-redis", "not_met"), # W = 0.45 * 1.35 = 0.6075
    ])

    # 25m session, tech_pool = 1500 - (120 + 0 + 210 + 225 + 180) = 765s
    config = create_test_policy_config(
        t_onboarding_base=120,
        n_onboarding=1,
        probe_pool_ratio=0.15,
        t_closing_reserve=180,
        coding_fallback_policy="downgrade_to_text",
        strict_priority_stop=True,
    )
    plan = derive_competency_plan(job=job, match=match, duration_minutes=25, policy_config=config)

    assert plan["techPoolSeconds"] == 765
    assert plan["unallocatedBufferSeconds"] == 0
    assert len(plan["targets"]) == 3

    targets_by_id = {t["conceptId"]: t for t in plan["targets"]}
    assert targets_by_id["concept-system-arch"]["timeEnvelopeSeconds"] == 276
    assert targets_by_id["concept-database-opt"]["timeEnvelopeSeconds"] == 260
    assert targets_by_id["concept-redis-cache"]["timeEnvelopeSeconds"] == 229

    # Exact sum check (276 + 260 + 229 == 765)
    total_env = sum(t["timeEnvelopeSeconds"] for t in plan["targets"])
    assert total_env == 765
    assert total_env + plan["unallocatedBufferSeconds"] == plan["techPoolSeconds"]


# --- TC-03: CODING Target Demands Higher Floor (360s) ---


def test_tc03_coding_target_floor_guarantee() -> None:
    """TC-03: CODING target receives minimum floor of 360s, while TEXT receives 180s."""
    c_code = SkillRequirement(
        requirementId="req-algo",
        priority="must_have",
        sourceEvidenceRef="jd-ev-algo",
        type="skill",
        skill=_concept("concept-algo-coding", "Algorithm Live Coding"),
    )
    object.__setattr__(c_code, "tags", ["coding_problem", "algorithm"])

    c_text = SkillRequirement(
        requirementId="req-arch",
        priority="must_have",
        sourceEvidenceRef="jd-ev-arch",
        type="skill",
        skill=_concept("concept-backend-design", "Backend Architecture"),
    )
    job = _job(c_code, c_text)
    match = _match([
        _result("req-algo", "unknown"), # W = 1.2
        _result("req-arch", "met"),     # W = 1.0
    ])

    config = create_test_policy_config(
        t_onboarding_base=120,
        n_onboarding=1,
        probe_pool_ratio=0.15,
        t_closing_reserve=180,
        coding_fallback_policy="downgrade_to_text",
        strict_priority_stop=True,
    )
    plan = derive_competency_plan(job=job, match=match, duration_minutes=25, policy_config=config)

    assert plan["techPoolSeconds"] == 765
    assert len(plan["targets"]) == 2
    by_id = {t["conceptId"]: t for t in plan["targets"]}
    assert by_id["concept-algo-coding"]["targetArchetype"] == "CODING"
    assert by_id["concept-algo-coding"]["floorSeconds"] == 360
    assert by_id["concept-algo-coding"]["timeEnvelopeSeconds"] == 483
    assert by_id["concept-algo-coding"]["estimatedQuestionsRange"] == [1, 2]

    assert by_id["concept-backend-design"]["targetArchetype"] == "TEXT"
    assert by_id["concept-backend-design"]["floorSeconds"] == 180
    assert by_id["concept-backend-design"]["timeEnvelopeSeconds"] == 282
    assert by_id["concept-backend-design"]["estimatedQuestionsRange"] == [1, 2]

    assert 483 + 282 == 765


# --- TC-04: Floor Omission when Budget is Insufficient for 4th Target ---


def test_tc04_budget_exhaustion_omits_lowest_ranked_target() -> None:
    """TC-04: 4 targets but pool only suffices for 3; 4th target is omitted."""
    reqs = [
        SkillRequirement(
            requirementId=f"req-{i}",
            priority="must_have" if i < 3 else "nice_to_have",
            sourceEvidenceRef=f"jd-ev-{i}",
            type="skill",
            skill=_concept(f"concept-skill-{i}", f"Skill {i}"),
        )
        for i in range(4)
    ]
    job = _job(*reqs)
    match = _match([_result(f"req-{i}", "unknown") for i in range(4)])

    # Duration with small tech pool (e.g. 550s): 3 targets need 3 * 180 = 540s, 4 targets need 720s
    # techPool = 550s -> can only fit 3 targets (540s floor), remaining 10s surplus
    config = create_test_policy_config(
        t_onboarding_base=120,
        n_onboarding=1,
        t_behavioral=100,
        probe_pool_ratio=0.15,
        t_closing_reserve=80,
        coding_fallback_policy="downgrade_to_text",
        strict_priority_stop=False,  # Skip-and-continue
    )
    # 900s total session: tech_pool = 900 - (120 + 0 + 100 + 135 + 80) = 465s (fits 2 targets floor 360s)
    # 18m = 1080s, probe = 0.15*1080 = 162s. techPool = 1080 - (120 + 0 + 100 + 162 + 80) = 618s (fits 3 targets: 540s, 78s surplus)
    plan = derive_competency_plan(job=job, match=match, duration_minutes=18, policy_config=config)

    assert len(plan["targets"]) == 3
    assert len(plan["nonInterviewedTargets"]) == 1
    omitted = plan["nonInterviewedTargets"][0]
    assert omitted["conceptId"] == "concept-skill-3"
    assert omitted["omissionReason"] == "insufficient_tech_pool_for_minimum_envelope"
    assert plan["unallocatedBufferSeconds"] == 0
    assert sum(t["timeEnvelopeSeconds"] for t in plan["targets"]) == plan["techPoolSeconds"]


# --- TC-05: Priority Target Fails CODING Floor (Policy Combinations) ---


def test_tc05_coding_fallback_5a_downgrade_to_text() -> None:
    """TC-05 Tổ hợp 1: strict_hands_on_required=False & 5A downgrade_to_text.

    Target coding downgrades to TEXT (floor 180s <= 240s) and is selected.
    Target 2 cannot fit (rem 60s < 180s) and is omitted.
    """
    c_code = SkillRequirement(
        requirementId="req-code",
        priority="must_have",
        sourceEvidenceRef="jd-ev-code",
        type="skill",
        skill=_concept("concept-algo", "Algorithm Problem"),
    )
    object.__setattr__(c_code, "tags", ["coding_problem"])

    c_text = SkillRequirement(
        requirementId="req-text",
        priority="nice_to_have",
        sourceEvidenceRef="jd-ev-text",
        type="skill",
        skill=_concept("concept-text", "Backend Theory"),
    )
    job = _job(c_code, c_text, strict_hands_on_required=False)
    match = _match([_result("req-code", "unknown"), _result("req-text", "unknown")])

    # Craft config to yield techPoolSeconds = 240s
    # total 900s (15m): 900 - (120 + 0 + 210 + 150 + 180) = 240s (probe 150 = 900 * 0.1666667)
    # Using 10m session (600s): 600 - (90 + 0 + 100 + 90 + 80) = 240s
    config = create_test_policy_config(
        t_onboarding_base=90,
        n_onboarding=1,
        t_behavioral=100,
        probe_pool_ratio=0.15,
        t_closing_reserve=80,
        coding_fallback_policy="downgrade_to_text",
        strict_priority_stop=True,
        strict_hands_on_required=False,
    )
    # 600 - (90 + 0 + 100 + 90 + 80) = 240s
    plan = derive_competency_plan(job=job, match=match, duration_minutes=10, policy_config=config)

    assert plan["techPoolSeconds"] == 240
    assert len(plan["targets"]) == 1
    t0 = plan["targets"][0]
    assert t0["conceptId"] == "concept-algo"
    assert t0["targetArchetype"] == "TEXT"  # Successfully downgraded!
    assert t0["timeEnvelopeSeconds"] == 240

    assert len(plan["nonInterviewedTargets"]) == 1
    omitted = plan["nonInterviewedTargets"][0]
    assert omitted["conceptId"] == "concept-text"
    assert omitted["omissionReason"] == "insufficient_tech_pool_for_minimum_envelope"


def test_tc05_coding_fallback_5b_omission_strict_priority_7a() -> None:
    """TC-05 Tổ hợp 2 Nhánh 2A: strict_hands_on_required=True & 5B Omission & 7A Strict Stop.

    Target 1 cannot downgrade (needs 360s > 240s) -> Omitted.
    7A halts immediately -> targets rỗng, full buffer 240s.
    """
    c_code = SkillRequirement(
        requirementId="req-code",
        priority="must_have",
        sourceEvidenceRef="jd-ev-code",
        type="skill",
        skill=_concept("concept-algo", "Algorithm Problem"),
    )
    object.__setattr__(c_code, "tags", ["coding_problem"])

    c_text = SkillRequirement(
        requirementId="req-text",
        priority="nice_to_have",
        sourceEvidenceRef="jd-ev-text",
        type="skill",
        skill=_concept("concept-text", "Backend Theory"),
    )
    job = _job(c_code, c_text, strict_hands_on_required=True)
    match = _match([_result("req-code", "unknown"), _result("req-text", "unknown")])

    config = create_test_policy_config(
        t_onboarding_base=90,
        n_onboarding=1,
        t_behavioral=100,
        probe_pool_ratio=0.15,
        t_closing_reserve=80,
        coding_fallback_policy="omit",
        strict_priority_stop=True,  # 7A Strict Stop
        strict_hands_on_required=True,
    )
    plan = derive_competency_plan(job=job, match=match, duration_minutes=10, policy_config=config)

    assert plan["techPoolSeconds"] == 240
    assert len(plan["targets"]) == 0
    assert plan["unallocatedBufferSeconds"] == 240
    assert len(plan["nonInterviewedTargets"]) == 2

    by_id = {item["conceptId"]: item for item in plan["nonInterviewedTargets"]}
    assert by_id["concept-algo"]["omissionReason"] == "insufficient_envelope_for_coding_assessment"
    assert by_id["concept-text"]["omissionReason"] == "strict_priority_halted_due_to_higher_rank"


def test_tc05_coding_fallback_5b_skip_and_continue_7b() -> None:
    """TC-05 Tổ hợp 2 Nhánh 2B: 5B Omission & 7B Skip-and-Continue.

    Target 1 cannot code (needs 360s > 240s) -> Omitted.
    7B skips to Target 2 (Text floor 180s <= 240s) -> Target 2 is selected with 240s.
    """
    c_code = SkillRequirement(
        requirementId="req-code",
        priority="must_have",
        sourceEvidenceRef="jd-ev-code",
        type="skill",
        skill=_concept("concept-algo", "Algorithm Problem"),
    )
    object.__setattr__(c_code, "tags", ["coding_problem"])

    c_text = SkillRequirement(
        requirementId="req-text",
        priority="nice_to_have",
        sourceEvidenceRef="jd-ev-text",
        type="skill",
        skill=_concept("concept-text", "Backend Theory"),
    )
    job = _job(c_code, c_text, strict_hands_on_required=True)
    match = _match([_result("req-code", "unknown"), _result("req-text", "unknown")])

    config = create_test_policy_config(
        t_onboarding_base=90,
        n_onboarding=1,
        t_behavioral=100,
        probe_pool_ratio=0.15,
        t_closing_reserve=80,
        coding_fallback_policy="omit",
        strict_priority_stop=False,  # 7B Skip-and-continue
        strict_hands_on_required=True,
    )
    plan = derive_competency_plan(job=job, match=match, duration_minutes=10, policy_config=config)

    assert plan["techPoolSeconds"] == 240
    assert len(plan["targets"]) == 1
    assert plan["targets"][0]["conceptId"] == "concept-text"
    assert plan["targets"][0]["timeEnvelopeSeconds"] == 240
    assert plan["unallocatedBufferSeconds"] == 0

    assert len(plan["nonInterviewedTargets"]) == 1
    assert plan["nonInterviewedTargets"][0]["conceptId"] == "concept-algo"
    assert plan["nonInterviewedTargets"][0]["omissionReason"] == "insufficient_envelope_for_coding_assessment"


def test_tc05_missing_hands_on_flag_defaults_safely_to_omission() -> None:
    """When strict_hands_on_required is missing on both Job and Config, do not assume False; omit safely."""
    c_code = SkillRequirement(
        requirementId="req-code",
        priority="must_have",
        sourceEvidenceRef="jd-ev-code",
        type="skill",
        skill=_concept("concept-algo", "Algorithm Problem"),
    )
    object.__setattr__(c_code, "tags", ["coding_problem"])
    job = _job(c_code)  # Missing strict_hands_on_required
    match = _match([_result("req-code", "unknown")])

    config = create_test_policy_config(
        t_onboarding_base=90,
        n_onboarding=1,
        t_behavioral=100,
        probe_pool_ratio=0.15,
        t_closing_reserve=80,
        coding_fallback_policy="downgrade_to_text",
        strict_priority_stop=True,
        strict_hands_on_required=None,  # Missing in config too
    )
    plan = derive_competency_plan(job=job, match=match, duration_minutes=10, policy_config=config)

    # Must NOT downgrade to text because missing flag != False
    assert len(plan["targets"]) == 0
    assert plan["unallocatedBufferSeconds"] == 240
    assert plan["nonInterviewedTargets"][0]["omissionReason"] == "insufficient_envelope_for_coding_assessment"


# --- TC-06: techPoolSeconds < minimum_envelope (K_eligible == 0) ---


def test_tc06_tech_pool_less_than_minimum_envelope() -> None:
    """TC-06: techPoolSeconds < 180s -> 0 targets eligible, full buffer, zero division safe."""
    c1 = SkillRequirement(
        requirementId="req-1",
        priority="must_have",
        sourceEvidenceRef="jd-ev-1",
        type="skill",
        skill=_concept("concept-1", "Skill 1"),
    )
    job = _job(c1)
    match = _match([_result("req-1", "unknown")])

    # Craft config to yield techPoolSeconds = 120s
    config = create_test_policy_config(
        t_onboarding_base=120,
        n_onboarding=1,
        t_behavioral=100,
        probe_pool_ratio=0.15,
        t_closing_reserve=170,
        coding_fallback_policy="downgrade_to_text",
        strict_priority_stop=True,
    )
    # 600 - (120 + 0 + 100 + 90 + 170) = 120s
    plan = derive_competency_plan(job=job, match=match, duration_minutes=10, policy_config=config)

    assert plan["techPoolSeconds"] == 120
    assert len(plan["targets"]) == 0
    assert plan["unallocatedBufferSeconds"] == 120
    assert len(plan["nonInterviewedTargets"]) == 1
    assert plan["nonInterviewedTargets"][0]["omissionReason"] == "insufficient_tech_pool_for_minimum_envelope"


def test_tech_pool_raises_value_error_when_reserves_exceed_session_duration() -> None:
    """When total non-tech reserves exceed sessionDurationSeconds, Planner must raise ValueError.

    Tech pool cannot be negative, and the planner must not silently clamp to zero.
    """
    c1 = SkillRequirement(
        requirementId="req-1",
        priority="must_have",
        sourceEvidenceRef="jd-ev-1",
        type="skill",
        skill=_concept("concept-1", "Skill 1"),
    )
    job = _job(c1)
    match = _match([_result("req-1", "unknown")])

    # 10 minutes = 600s total duration
    # Config with reserves = 120 (onboarding) + 0 + 300 (behavioral) + 120 (probe 0.20*600) + 180 (closing) = 720s > 600s
    config = create_test_policy_config(
        t_onboarding_base=120,
        n_onboarding=1,
        t_behavioral=300,
        probe_pool_ratio=0.20,
        t_closing_reserve=180,
        coding_fallback_policy="downgrade_to_text",
        strict_priority_stop=True,
    )
    with pytest.raises(ValueError, match="total reserves .* exceed session duration"):
        derive_competency_plan(job=job, match=match, duration_minutes=10, policy_config=config)


def test_tech_pool_zero_allowed_and_never_negative() -> None:
    """When reserves exactly equal session duration, techPoolSeconds is 0 (allowed input).

    Neither techPoolSeconds nor unallocatedBufferSeconds can be negative; targets cannot fit floor and are omitted.
    """
    c1 = SkillRequirement(
        requirementId="req-1",
        priority="must_have",
        sourceEvidenceRef="jd-ev-1",
        type="skill",
        skill=_concept("concept-1", "Skill 1"),
    )
    job = _job(c1)
    match = _match([_result("req-1", "unknown")])

    # 10 minutes = 600s total duration
    # Config with reserves summing exactly to 600s:
    # 120 (onboarding) + 0 (cv) + 250 (behavioral) + 120 (probe 0.20*600) + 110 (closing) = 600s
    config = create_test_policy_config(
        t_onboarding_base=120,
        n_onboarding=1,
        t_behavioral=250,
        probe_pool_ratio=0.20,
        t_closing_reserve=110,
        coding_fallback_policy="downgrade_to_text",
        strict_priority_stop=True,
    )
    plan = derive_competency_plan(job=job, match=match, duration_minutes=10, policy_config=config)

    assert plan["techPoolSeconds"] == 0
    assert plan["unallocatedBufferSeconds"] == 0
    assert len(plan["targets"]) == 0
    assert len(plan["nonInterviewedTargets"]) == 1
    assert plan["nonInterviewedTargets"][0]["omissionReason"] in (
        "insufficient_tech_pool_for_minimum_envelope",
        "strict_priority_halted_due_to_higher_rank",
    )


# --- TC-07: Candidate Pre-Filtering of 100% not_applicable Targets ---


def test_tc07_candidate_prefiltering_with_valid_remaining_targets() -> None:
    """TC-07: not_applicable target is pre-filtered to nonInterviewedTargets; valid targets allocated."""
    req_na = SkillRequirement(
        requirementId="req-legacy-soap",
        priority="nice_to_have",
        sourceEvidenceRef="jd-ev-soap",
        type="skill",
        skill=_concept("concept-soap", "SOAP Legacy"),
    )
    req_1 = SkillRequirement(
        requirementId="req-arch",
        priority="must_have",
        sourceEvidenceRef="jd-ev-arch",
        type="skill",
        skill=_concept("concept-arch", "System Architecture"),
    )
    req_2 = SkillRequirement(
        requirementId="req-db",
        priority="must_have",
        sourceEvidenceRef="jd-ev-db",
        type="skill",
        skill=_concept("concept-db", "Database Tuning"),
    )
    job = _job(req_na, req_1, req_2)
    match = _match([
        _result("req-legacy-soap", "not_applicable"),
        _result("req-arch", "unknown"),  # W = 1.2
        _result("req-db", "met"),        # W = 1.0
    ])

    config = create_test_policy_config(
        t_onboarding_base=90,
        n_onboarding=1,
        t_behavioral=100,
        probe_pool_ratio=0.15,
        t_closing_reserve=90,
        coding_fallback_policy="downgrade_to_text",
        strict_priority_stop=True,
    )
    # 770s total: 770 - (90 + 0 + 100 + 116 + 90) = 374s (probe = round(0.15*770) = 116)
    # Let's use 15m (900s): techPool = 900 - (90+0+100+135+90) = 485s
    plan = derive_competency_plan(job=job, match=match, duration_minutes=15, policy_config=config)

    assert len(plan["targets"]) == 2
    assert len(plan["nonInterviewedTargets"]) == 1
    omitted = plan["nonInterviewedTargets"][0]
    assert omitted["conceptId"] == "concept-soap"
    assert omitted["omissionReason"] == "not_applicable_for_candidate"
    assert omitted["omissionDetail"]["matchStatus"] == "not_applicable"

    # Evaluation targets must retain req-legacy-soap
    eval_targets_by_id = {item["requirementId"]: item for item in plan["evaluationTargets"]}
    assert eval_targets_by_id["req-legacy-soap"]["status"] == "not_applicable"
    assert eval_targets_by_id["req-legacy-soap"]["attention"] == "not_applicable"

    # Invariant 4B partition
    total_canonical = len(plan["targets"]) + len(plan["nonInterviewedTargets"])
    assert total_canonical == 3


# --- TC-08: Defensive Equal-Share Allocation Fallback (Zero Total Weight) ---


def test_tc08_defensive_equal_share_allocation_when_total_weight_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-08: Defensive zero-weight fixture: all candidate weights = 0 -> split surplus equally without ZeroDivisionError.

    Injects zero priority weight so sum(raw_weight) is strictly 0.0.
    Verifies:
    1. No ZeroDivisionError occurs.
    2. Envelope is distributed equally on integer seconds.
    3. Remainder seconds are distributed deterministically by index tie-break.
    4. Exact total pool conservation and zero buffer.
    """
    monkeypatch.setattr(
        planner,
        "_PRIORITY_WEIGHT",
        {
            "must_have": 0.0,
            "nice_to_have": 0.0,
            "context": 0.0,
        },
    )

    c1 = SkillRequirement(
        requirementId="req-1",
        priority="must_have",
        sourceEvidenceRef="jd-ev-1",
        type="skill",
        skill=_concept("concept-1", "Concept 1"),
    )
    c2 = SkillRequirement(
        requirementId="req-2",
        priority="must_have",
        sourceEvidenceRef="jd-ev-2",
        type="skill",
        skill=_concept("concept-2", "Concept 2"),
    )
    c3 = SkillRequirement(
        requirementId="req-3",
        priority="must_have",
        sourceEvidenceRef="jd-ev-3",
        type="skill",
        skill=_concept("concept-3", "Concept 3"),
    )
    job = _job(c1, c2, c3)
    match = _match([
        _result("req-1", "unknown"),
        _result("req-2", "unknown"),
        _result("req-3", "unknown"),
    ])

    # 20m = 1200s session
    # probe = round(0.15 * 1200) = 180s
    # total reserves = 90 + 0 + 100 + 180 + 189 = 559s
    # techPool = 1200 - 559 = 641s
    # 3 TEXT targets each have floor 180s -> 3 * 180 = 540s
    # surplus = 641 - 540 = 101s
    # Equal share: 101 // 3 = 33s each, remainder = 2s
    # Deterministic index tie-break awards +1s to first 2 targets:
    # Target 0: 180 + 33 + 1 = 214s
    # Target 1: 180 + 33 + 1 = 214s
    # Target 2: 180 + 33 + 0 = 213s
    config = create_test_policy_config(
        t_onboarding_base=90,
        n_onboarding=1,
        t_behavioral=100,
        probe_pool_ratio=0.15,
        t_closing_reserve=189,
        coding_fallback_policy="downgrade_to_text",
        strict_priority_stop=True,
    )
    plan = derive_competency_plan(job=job, match=match, duration_minutes=20, policy_config=config)

    assert len(plan["targets"]) == 3
    assert plan["techPoolSeconds"] == 641
    assert plan["unallocatedBufferSeconds"] == 0

    t0, t1, t2 = plan["targets"]
    assert t0["timeEnvelopeSeconds"] == 214
    assert t1["timeEnvelopeSeconds"] == 214
    assert t2["timeEnvelopeSeconds"] == 213

    # Normalized weights are equal share (1/3 each), not NaN or ZeroDivisionError
    for t in plan["targets"]:
        assert t["importance"] == round(1.0 / 3, 6)

    # Invariants
    assert sum(t["timeEnvelopeSeconds"] for t in plan["targets"]) + plan["unallocatedBufferSeconds"] == plan["techPoolSeconds"]


# --- Option B: 100% of Competency Targets are not_applicable ---


def test_option_b_all_targets_not_applicable_returns_full_traceability_plan() -> None:
    """Option B: When 100% of requirements are not_applicable, Planner does NOT raise ValueError.

    It returns a full plan with targets=[], buffer=techPoolSeconds, and full traceability.
    """
    req = SkillRequirement(
        requirementId="req-legacy",
        priority="must_have",
        sourceEvidenceRef="jd-ev-legacy",
        type="skill",
        skill=_concept("concept-legacy", "Legacy Tool"),
    )
    job = _job(req)
    match = _match([_result("req-legacy", "not_applicable")])

    config = create_test_policy_config(
        t_onboarding_base=120,
        n_onboarding=1,
        coding_fallback_policy="downgrade_to_text",
        strict_priority_stop=True,
    )
    plan = derive_competency_plan(job=job, match=match, duration_minutes=15, policy_config=config)

    assert plan["policyVersion"] == PLANNER_POLICY_VERSION_DYNAMIC
    assert plan["targets"] == []
    assert plan["unallocatedBufferSeconds"] == plan["techPoolSeconds"]
    assert len(plan["nonInterviewedTargets"]) == 1
    assert plan["nonInterviewedTargets"][0]["conceptId"] == "concept-legacy"
    assert plan["nonInterviewedTargets"][0]["omissionReason"] == "not_applicable_for_candidate"

    assert len(plan["evaluationTargets"]) == 1
    assert plan["evaluationTargets"][0]["requirementId"] == "req-legacy"
    assert plan["evaluationTargets"][0]["status"] == "not_applicable"


# --- Invariant 5: Decoupling from Question Bank ---


def test_invariant_5_zero_question_bank_dependency() -> None:
    """Planner output payload must NOT contain question_version_id or rubric_version_id."""
    req = SkillRequirement(
        requirementId="req-python",
        priority="must_have",
        sourceEvidenceRef="jd-ev-py",
        type="skill",
        skill=_concept("concept-python", "Python Core"),
    )
    job = _job(req)
    match = _match([_result("req-python", "unknown")])

    config = create_test_policy_config(
        t_onboarding_base=120,
        n_onboarding=1,
        coding_fallback_policy="downgrade_to_text",
        strict_priority_stop=True,
    )
    plan = derive_competency_plan(job=job, match=match, duration_minutes=15, policy_config=config)

    assert "question_version_id" not in str(plan)
    assert "rubric_version_id" not in str(plan)
    assert "questionVersionId" not in str(plan)
    assert "rubricVersionId" not in str(plan)


# --- Legacy Backward Compatibility ---


def test_legacy_backward_compatibility_when_policy_config_is_none() -> None:
    """When policy_config=None, Planner executes legacy algorithm without Gate 2 dynamic fields."""
    req = SkillRequirement(
        requirementId="req-python",
        priority="must_have",
        sourceEvidenceRef="jd-ev-py",
        type="skill",
        skill=_concept("concept-python", "Python Core"),
    )
    job = _job(req)
    match = _match([_result("req-python", "unknown")])

    plan = derive_competency_plan(job=job, match=match, duration_minutes=25, policy_config=None)

    assert plan["policyVersion"] == PLANNER_POLICY_VERSION  # interview-planner-v1
    assert "questionBudget" in plan
    assert "techPoolSeconds" not in plan  # Legacy does not compute techPoolSeconds
    assert "nonInterviewedTargets" not in plan
