"""Re-evaluate a match after candidate clarification answers."""

from fastapi.concurrency import run_in_threadpool

from src.modules.matching.application.facade import get_matching_facade
from src.modules.matching.clarifications.answer_evidence import (
    build_candidate_answer_evidence,
    candidate_negative_result,
    is_explicit_negative,
    language_claim_from_answer,
    skill_claim_from_answer,
)
from src.modules.matching.clarifications.models import (
    ClarificationAnswerOutcome,
    MatchClarificationRescoreRequest,
    MatchClarificationRescoreResult,
)
from src.modules.matching.domain.schemas import (
    LanguageRequirement,
    RequirementResult,
    SkillRequirement,
)
from src.modules.user_cvs.domain.schemas import EvidenceSpan, LanguageClaim, SkillClaim


async def rescore_match_with_clarification_answers(
    payload: MatchClarificationRescoreRequest,
) -> MatchClarificationRescoreResult:
    """Re-run matching with requirement-scoped, explicitly self-reported evidence."""
    request = payload.match_request
    facade = get_matching_facade()
    # Never trust the client-returned scores or requirement statuses. Recompute
    # the baseline from the submitted canonical CV/JD before accepting answers.
    initial = await run_in_threadpool(facade.match, request)
    initial_ids = {item.requirement_id for item in initial.requirement_results}
    asked_ids = {item.requirement_id for item in payload.initial_analysis.clarification_requests}
    unknown_ids = {
        item.requirement_id
        for item in initial.requirement_results
        if item.status == "unknown"
    }
    if not asked_ids.issubset(initial_ids) or not asked_ids.issubset(unknown_ids):
        raise ValueError("clarifications must target requirements currently assessed as unknown")
    if not {item.requirement_id for item in payload.answers}.issubset(asked_ids):
        raise ValueError("each answer must target a returned clarification question")
    requirements_by_id = {item.requirement_id: item for item in request.job.requirements}
    initial_by_id = {item.requirement_id: item for item in initial.requirement_results}
    evidence_by_requirement: dict[str, EvidenceSpan] = {}
    skill_claims: list[SkillClaim] = []
    language_claims: list[LanguageClaim] = []
    overrides: dict[str, RequirementResult] = {}

    for answer in payload.answers:
        requirement = requirements_by_id[answer.requirement_id]
        evidence = build_candidate_answer_evidence(answer.requirement_id, answer.answer_text)
        evidence_by_requirement[answer.requirement_id] = evidence
        negative = is_explicit_negative(answer.answer_text)

        if negative and isinstance(requirement, (SkillRequirement, LanguageRequirement)):
            if isinstance(requirement, SkillRequirement) and requirement.operator == "gte":
                claim = skill_claim_from_answer(requirement, answer.answer_text, evidence)
                if claim is not None:
                    skill_claims.append(claim)
            elif isinstance(requirement, SkillRequirement) and requirement.operator == "proficiency_gte":
                overrides[requirement.requirement_id] = candidate_negative_result(requirement, evidence)
            elif isinstance(requirement, SkillRequirement):
                overrides[requirement.requirement_id] = candidate_negative_result(requirement, evidence)
            else:
                overrides[requirement.requirement_id] = candidate_negative_result(requirement, evidence)
            continue

        skill_claim = skill_claim_from_answer(requirement, answer.answer_text, evidence)
        if skill_claim is not None:
            skill_claims.append(skill_claim)
        language_claim = language_claim_from_answer(requirement, answer.answer_text, evidence)
        if language_claim is not None:
            language_claims.append(language_claim)

    answer_evidence = list(evidence_by_requirement.values())
    resume = request.resume.model_copy(
        update={
            "evidence": [*request.resume.evidence, *answer_evidence],
            "skills": [*request.resume.skills, *skill_claims],
            "languages": [*request.resume.languages, *language_claims],
        }
    )
    rescoring_request = request.model_copy(update={"resume": resume, "async_processing": False})

    # Preserve every unasked result and keep unresolved answers unresolved unless
    # the matching evaluator actually used that answer's evidence.
    answer_ids = set(evidence_by_requirement)
    for requirement_id, original_result in initial_by_id.items():
        if requirement_id not in answer_ids:
            overrides[requirement_id] = original_result

    rescored = await run_in_threadpool(
        facade.match,
        rescoring_request,
        requirement_overrides=overrides,
    )
    self_report_refs = {item.evidence_id for item in answer_evidence}
    final_by_id = {item.requirement_id: item for item in rescored.requirement_results}
    final_overrides = dict(initial_by_id)
    used_self_report = False
    for answer in payload.answers:
        result = final_by_id[answer.requirement_id]
        evidence = evidence_by_requirement[answer.requirement_id]
        if evidence.evidence_id not in result.evidence_refs:
            continue
        evidence_sources = {
            ref: "candidate_self_report" if ref in self_report_refs else "document"
            for ref in result.evidence_refs
            if ref in self_report_refs
            or any(item.evidence_id == ref for item in request.resume.evidence)
        }
        if any(source == "candidate_self_report" for source in evidence_sources.values()):
            result = result.model_copy(
                update={
                    "confidence": min(result.confidence, 0.72),
                    "evidence_sources": evidence_sources,
                    "evidence_explanation": (
                        (result.evidence_explanation + " ")
                        if result.evidence_explanation
                        else ""
                    )
                    + (
                        "Phần kết luận này có sử dụng câu trả lời tự khai của ứng viên, "
                        "chưa được xác minh độc lập."
                    ),
                }
            )
            used_self_report = True
        else:
            result = result.model_copy(update={"evidence_sources": evidence_sources})
        final_overrides[answer.requirement_id] = result

    # Apply the accepted per-requirement outcomes to a clean CV-only scoring
    # pass. Candidate answers cannot leak into global semantic factors or other
    # requirements; all aggregate scores are recalculated from the final rows.
    final_match = await run_in_threadpool(
        facade.match,
        request,
        requirement_overrides=final_overrides,
    )
    if used_self_report:
        final_match = final_match.model_copy(
            update={"warnings": list(dict.fromkeys([*final_match.warnings, "candidate_self_report_used"]))}
        )
    final_statuses = {item.requirement_id: item.status for item in final_match.requirement_results}
    outcomes = [
        ClarificationAnswerOutcome(
            requirementId=answer.requirement_id,
            evidenceRef=evidence_by_requirement[answer.requirement_id].evidence_id,
            status=final_statuses[answer.requirement_id],
        )
        for answer in payload.answers
    ]
    return MatchClarificationRescoreResult(
        initialMatchResult=initial,
        finalMatchResult=final_match,
        processedAnswers=outcomes,
    )
