from typing import Literal

from src.modules.job_descriptions.schemas import (
    CanonicalJobDescription,
    JobRequirement,
    resolve_known_skill_concepts,
)
from src.modules.matching.schemas import (
    CanonicalJob,
    GroundedJobText,
    SkillRequirement,
    UnresolvedRequirement,
)

_PRIORITY_MAP: dict[str, Literal["must_have", "nice_to_have"]] = {
    # Official canonical JD contract (CanonicalJobDescription)
    "must_have": "must_have",
    "preferred": "nice_to_have",
    # Backward compatibility aliases
    "required": "must_have",
    "nice_to_have": "nice_to_have",
}


def _canonical_requirements(parsed: CanonicalJobDescription) -> list[JobRequirement]:
    """Select one parser source for legacy hybrid artifacts.

    hybrid-jd-v2 used to persist the deterministic baseline followed by an
    independently paraphrased ``req-llm-*`` set.  Source selection is based on
    explicit parser provenance in the IDs, never fuzzy display-text matching.
    New parser output merges the deterministic set with only genuinely new
    grounded claims, so discarding the baseline would silently reduce a full
    JD to one requirement.
    """
    llm_requirements = [
        item for item in parsed.requirements if item.requirement_id.startswith("req-llm-")
    ]
    baseline_requirements = [
        item for item in parsed.requirements if not item.requirement_id.startswith("req-llm-")
    ]
    if (
        llm_requirements
        and baseline_requirements
        and parsed.parsing.parser_version == "hybrid-jd-v2"
    ):
        return llm_requirements
    return parsed.requirements


def job_description_to_matching_job(
    parsed: CanonicalJobDescription,
    *,
    job_id: str,
) -> CanonicalJob:
    """Preserve finalized JD fields and fail closed when evidence is unavailable."""
    if not parsed.evidence:
        raise ValueError("a finalized JD needs document evidence before matching")
    first_evidence = parsed.evidence[0]
    requirements = []
    for requirement in _canonical_requirements(parsed):
        if not requirement.evidence_refs:
            raise ValueError(f"requirement {requirement.requirement_id} has no evidence")
        priority = _PRIORITY_MAP.get(requirement.priority)
        if priority is None:
            raise ValueError(
                f"unsupported requirement priority '{requirement.priority}' "
                f"for requirement '{requirement.requirement_id}'"
            )
        common = {
            "requirementId": requirement.requirement_id,
            "priority": priority,
            "sourceEvidenceRef": requirement.evidence_refs[0],
        }

        # Older finalized JDs may predate taxonomy persistence even though their
        # evidence-grounded raw requirement text contains an exact alias from
        # the same closed taxonomy used by the deterministic parser. Recover
        # only those explicit known aliases; never fuzzy-map arbitrary text.
        recovered_concepts = (
            resolve_known_skill_concepts(requirement.raw_label)
            if requirement.kind == "skill"
            and requirement.concept is None
            and not requirement.atomic_concepts
            else []
        )
        concept = requirement.concept
        atomic_concepts = requirement.atomic_concepts
        recovered_group_operator = requirement.group_operator
        if recovered_concepts:
            atomic_concepts = recovered_concepts
            if len(recovered_concepts) > 1:
                # Recovery is based on explicit aliases co-occurring in one
                # evidence-grounded skill requirement.  Legacy records did not
                # persist composition metadata, so represent the recovered
                # decomposition as all_of rather than emitting an invalid
                # multi-concept contract.
                recovered_group_operator = recovered_group_operator or "all_of"

        if (
            requirement.kind == "skill"
            and concept is not None
            and not atomic_concepts
        ):
            requirements.append(
                SkillRequirement(
                    **common,
                    type="skill",
                    skill=requirement.concept,
                    rawLabel=requirement.raw_label,
                    operator="gte" if requirement.minimum_experience_months is not None else "required",
                    minimumExperienceMonths=requirement.minimum_experience_months,
                )
            )
        else:
            requirements.append(
                UnresolvedRequirement(
                    **common,
                    type="unresolved",
                    kind=requirement.kind,
                    rawLabel=requirement.raw_label,
                    atomicConcepts=atomic_concepts,
                    minimumExperienceMonths=requirement.minimum_experience_months,
                    operator=requirement.operator,
                    threshold=requirement.threshold,
                    scale=requirement.scale,
                    credential=requirement.credential,
                    equivalentAllowed=requirement.equivalent_allowed,
                    groupId=requirement.group_id,
                    groupOperator=recovered_group_operator,
                )
            )

    return CanonicalJob(
        schemaVersion="2.1",
        jobId=job_id,
        documentId=first_evidence.document_id,
        documentSha256=first_evidence.document_sha256,
        jobTitle=parsed.job_title,
        careerClassifications=parsed.career_classifications,
        seniority=parsed.seniority,
        employmentType=parsed.employment_type,
        workMode=parsed.work_mode,
        location=parsed.location,
        responsibilities=[
            GroundedJobText(text=item.text, evidenceRefs=item.evidence_refs)
            for item in parsed.responsibilities
        ],
        benefits=[
            GroundedJobText(text=item.text, evidenceRefs=item.evidence_refs)
            for item in parsed.benefits
        ],
        requirements=requirements,
        evidence=parsed.evidence,
    )
