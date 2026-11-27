"""Combine a rule verdict with the judge's reading of the same requirement.

The policy is one table, and it is asymmetric on purpose:

    judge says            rules say              result
    ----------------------------------------------------------------
    satisfies (grounded)  anything but met       met, citing the judge's spans
    satisfies (grounded)  met                    the rule result, unchanged
    partially / unsure    anything               the rule result, unchanged
    unrelated             anything               the rule result, unchanged
    no reading            anything               the rule result, unchanged

A grounded ``satisfies`` can raise a verdict.  Nothing the judge says can
lower one: ``not_met`` keeps its single source, the rules confirming that a
complete CV holds nothing.  A model that is unsure because the wording was
unfamiliar must not reject anyone for it, and ``unknown`` already exists for
exactly that case, with a clarification step behind it.

Numeric requirements - a GPA threshold, a credential score - take the other
path, ``evaluate_with_numeric``: the numeric judge extracts the number with a
verbatim quote, and the rule evaluator is run again over the CV with that
span restated as "GPA 3.25/4.0" or "IELTS 6.5".  The rules keep the one copy
of every comparison, and may now say ``not_met`` when the number they were
handed is short of the floor: the number was verified character by character
against the CV, so the rejection is still theirs.  Only a rule verdict that
found no number at all is replaced; a rule that read a number keeps its say.
"""

from __future__ import annotations

from src.core.trace_logging import trace_event
from src.modules.matching.domain.schemas import RequirementResult, UnresolvedRequirement
from src.modules.matching.evaluation.legacy_rules import EvaluatorSelection
from src.modules.matching.evaluation.legacy_rules import (
    evaluate_unresolved_requirement as evaluate_by_rules,
)
from src.modules.matching.judge.readings import ReadingSet
from src.modules.user_cvs.schemas import CanonicalResume

JUDGE_REASON_CODE = "requirement_judged_satisfied"
JUDGE_CONFIDENCE = 0.85

# Rule verdicts that mean "no number was read", the only ones a numeric
# reading may replace.  A rule that read a number and found it short keeps
# its verdict; the judge is not a second opinion on a comparison.
NUMERIC_NOT_FOUND = frozenset(
    {
        "gpa_not_evidenced",
        "gpa_evidence_missing",
        "language_credential_not_evidenced",
        "language_credential_evidence_missing",
        "requirement_not_found",
    }
)

# Evaluators whose question is "do these spans show this capability", which
# is what the judge answers.  The numeric evaluators are not here.
ENTAILMENT_EVALUATORS = frozenset(
    {
        "knowledge_skill",
        "programming_foundation",
        "research_or_competition",
        "ai_activity",
        "education_status",
        "certification",
        "generic_requirement",
    }
)


def evaluate_with_readings(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
    readings: ReadingSet,
    rule_result: RequirementResult,
    selection: EvaluatorSelection | None,
) -> RequirementResult:
    reading = readings.reading_for(requirement.requirement_id)
    evaluator = selection.name if selection is not None else "generic_requirement"
    eligible = evaluator in ENTAILMENT_EVALUATORS
    applied = (
        reading is not None
        and reading.relation == "satisfies"
        and eligible
        and rule_result.status in {"not_met", "unknown"}
    )
    trace_event(
        "judge",
        "judge_verdict",
        requirement_id=requirement.requirement_id,
        evaluator=evaluator,
        eligible=eligible,
        rule_status=rule_result.status,
        rule_reason_code=rule_result.reason_code,
        judge_relation=None if reading is None else reading.relation,
        judge_selected=[] if reading is None else [item.evidence_id for item in reading.selected],
        fallback=readings.fallbacks.get(requirement.requirement_id),
        agree=_agree(rule_result.status, reading),
        applied=applied,
    )
    if not applied:
        return rule_result
    assert reading is not None
    present = {item.evidence_id: item for item in resume.evidence}
    refs = [item.evidence_id for item in reading.selected if item.evidence_id in present]
    if not refs:
        return rule_result
    quotes = "; ".join(f"“{item.quote}”" for item in reading.selected if item.evidence_id in present)
    sources = {ref: present[ref].evidence_source for ref in refs}
    # A quote from the candidate's reply is not "the CV", and the reader must
    # not be told it is.  Document-only readings keep their original wording.
    origin = (
        "Câu trả lời của ứng viên có nội dung đáp ứng yêu cầu"
        if "candidate_self_report" in sources.values()
        else "CV có nội dung đáp ứng yêu cầu"
    )
    return RequirementResult(
        requirementId=requirement.requirement_id,
        status="met",
        score=1.0,
        confidence=JUDGE_CONFIDENCE,
        evidenceRefs=refs,
        evidenceSources=sources,
        reasonCode=JUDGE_REASON_CODE,
        evidenceExplanation=f"{origin}: {quotes}.",
        groupOperator=rule_result.group_operator,
    )


def evaluate_with_numeric(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
    readings: ReadingSet,
    rule_result: RequirementResult,
) -> RequirementResult:
    """Re-run the rules over the CV with the judge's number restated in its span."""
    reading = readings.numeric_for(requirement.requirement_id)
    present = {item.evidence_id: item for item in resume.evidence}
    applicable = (
        reading is not None
        and rule_result.reason_code in NUMERIC_NOT_FOUND
        and reading.evidence_id in present
    )
    result = rule_result
    if applicable:
        assert reading is not None
        restated = resume.model_copy(
            update={
                "evidence": [
                    span.model_copy(update={"text": reading.restated()})
                    if span.evidence_id == reading.evidence_id
                    else span
                    for span in resume.evidence
                ]
            }
        )
        rerun = evaluate_by_rules(requirement, restated)
        if reading.evidence_id in rerun.evidence_refs:
            result = rerun.model_copy(
                update={
                    "confidence": min(rerun.confidence, JUDGE_CONFIDENCE),
                    "evidence_explanation": (
                        f"Đọc từ CV: “{reading.quote}” (hiểu là {reading.restated()}). "
                        f"{rerun.evidence_explanation or ''}"
                    ).strip(),
                }
            )
    trace_event(
        "judge",
        "numeric_verdict",
        requirement_id=requirement.requirement_id,
        rule_status=rule_result.status,
        rule_reason_code=rule_result.reason_code,
        reading=None if reading is None else reading.restated(),
        fallback=readings.fallbacks.get(requirement.requirement_id),
        applied=result is not rule_result,
        status=result.status,
        reason_code=result.reason_code,
    )
    return result


def _agree(rule_status: str, reading) -> bool | None:
    """Whether the two readings point the same way; ``None`` when there is nothing to compare."""

    if reading is None or reading.relation == "unsure":
        return None
    if reading.relation == "satisfies":
        return rule_status == "met"
    if reading.relation == "unrelated":
        return rule_status == "not_met"
    return rule_status == "unknown"
