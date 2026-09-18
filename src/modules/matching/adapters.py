"""Adapters from finalized parser contracts to the matching contract."""

from src.modules.job_descriptions.schemas import CanonicalJobDescription
from src.modules.matching.schemas import (
    CanonicalJob,
    GroundedJobText,
    SkillRequirement,
    UnresolvedRequirement,
)


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
    for requirement in parsed.requirements:
        if not requirement.evidence_refs:
            raise ValueError(f"requirement {requirement.requirement_id} has no evidence")
        common = {
            "requirementId": requirement.requirement_id,
            "priority": "nice_to_have" if requirement.priority == "preferred" else "must_have",
            "sourceEvidenceRef": requirement.evidence_refs[0],
        }
        if requirement.kind == "skill" and requirement.concept is not None:
            requirements.append(
                SkillRequirement(
                    **common,
                    type="skill",
                    skill=requirement.concept,
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
                    minimumExperienceMonths=requirement.minimum_experience_months,
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
