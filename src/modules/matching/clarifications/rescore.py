"""Re-evaluate a match after candidate clarification answers."""

import time
from collections import Counter
from collections.abc import Mapping

from fastapi.concurrency import run_in_threadpool

from src.core.trace_logging import trace_event
from src.modules.matching.application.facade import get_matching_facade
from src.modules.matching.clarifications.answer_evidence import (
    build_candidate_answer_evidence,
    candidate_negative_result,
    candidate_self_report_result,
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
    RequirementResult,
    SkillRequirement,
)
from src.modules.user_cvs.schemas import EvidenceSpan, LanguageClaim, SkillClaim


async def rescore_match_with_clarification_answers(
    payload: MatchClarificationRescoreRequest,
    *,
    trusted_questions: Mapping[str, str] | None = None,
) -> MatchClarificationRescoreResult:
    """Re-run matching with requirement-scoped, explicitly self-reported evidence.

    ``trusted_questions`` maps a requirement id to the question this server
    asked for it, taken from the server's own record - never from the request
    body, whose ``initial_analysis`` the client wrote.  The judge reads each
    answer beside its question; a bare "Dạ rồi" is unreadable without one and
    the judge was right to say so.  Only the pass that holds the answers gets
    them; the baseline and the final pass see the CV alone, as before.
    """
    started = time.perf_counter()
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

        # "Tôi không biết" is an answer, and it answers no.  The negative
        # branch used to apply only to skill and language requirements, so for
        # every other kind the reply fell through to ordinary evaluation - and
        # once the answer was cited there at all, the requirement moved from
        # ``not_met`` to ``unknown``, which raised the score for saying no.
        if negative:
            if isinstance(requirement, SkillRequirement) and requirement.operator == "gte":
                claim = skill_claim_from_answer(requirement, answer.answer_text, evidence)
                if claim is not None:
                    skill_claims.append(claim)
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

    answered_prompts = {
        requirement_id: question
        for requirement_id, question in (trusted_questions or {}).items()
        if requirement_id in evidence_by_requirement
    }
    rescored = await run_in_threadpool(
        facade.match,
        rescoring_request,
        requirement_overrides=overrides,
        clarification_prompts=answered_prompts or None,
    )
    self_report_refs = {item.evidence_id for item in answer_evidence}
    final_by_id = {item.requirement_id: item for item in rescored.requirement_results}
    final_overrides = dict(initial_by_id)
    used_self_report = False
    unused_answers: list[str] = []
    for answer in payload.answers:
        result = final_by_id[answer.requirement_id]
        evidence = evidence_by_requirement[answer.requirement_id]
        if evidence.evidence_id not in result.evidence_refs or result.status == "unknown":
            # The requirement's own evaluator could not use this answer.  Four
            # evaluator families read structured CV fields alone - the
            # employment timeline, a GPA, a certification list, a degree - so
            # an answer about any of them was never cited and the requirement
            # came back exactly as it went in.  Falling through to the initial
            # result here is what made the whole step look broken: a question
            # was asked, an answer was typed, and nothing moved.  A self-report
            # verdict is the floor for that, capped and tagged so it can never
            # pass for a read of the CV.
            fallback = candidate_self_report_result(
                requirements_by_id[answer.requirement_id], answer.answer_text, evidence
            )
            if fallback is not None:
                final_overrides[answer.requirement_id] = fallback
                if fallback.reason_code != "candidate_self_report_unclear":
                    used_self_report = True
                else:
                    unused_answers.append(answer.requirement_id)
                continue
            unused_answers.append(answer.requirement_id)
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
    final_rows = {item.requirement_id: item for item in final_match.requirement_results}
    final_statuses = {key: row.status for key, row in final_rows.items()}
    outcomes = [
        ClarificationAnswerOutcome(
            requirementId=answer.requirement_id,
            evidenceRef=evidence_by_requirement[answer.requirement_id].evidence_id,
            status=final_statuses[answer.requirement_id],
            reasonCode=final_rows[answer.requirement_id].reason_code,
            explanation=final_rows[answer.requirement_id].evidence_explanation,
        )
        for answer in payload.answers
    ]
    initial_statuses = {item.requirement_id: item.status for item in initial.requirement_results}
    transitions = Counter(
        f"{initial_statuses.get(answer.requirement_id)}->{final_statuses[answer.requirement_id]}"
        for answer in payload.answers
    )
    trace_event(
        "clarification",
        "rescore_completed",
        answers_count=len(payload.answers),
        transitions=dict(transitions),
        suitability_before=initial.suitability_score,
        suitability_after=final_match.suitability_score,
        fit_band_before=initial.fit_band,
        fit_band_after=final_match.fit_band,
        passes=3,
        self_report_used=used_self_report,
        unusable_answer_count=len(unused_answers),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    return MatchClarificationRescoreResult(
        initialMatchResult=initial,
        finalMatchResult=final_match,
        processedAnswers=outcomes,
    )
