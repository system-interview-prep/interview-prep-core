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

Numeric requirements - a GPA threshold, a credential score, years of
experience, a degree level - are left to the rules in this version.  The
judge can say the spans are about the right thing; it cannot be trusted to
compare the numbers, and the comparators that would check its extraction
are not wired yet.
"""

from __future__ import annotations

from src.core.trace_logging import trace_event
from src.modules.matching.domain.schemas import RequirementResult, UnresolvedRequirement
from src.modules.matching.evaluation.legacy_rules import EvaluatorSelection
from src.modules.matching.judge.readings import ReadingSet
from src.modules.user_cvs.schemas import CanonicalResume

JUDGE_REASON_CODE = "requirement_judged_satisfied"
JUDGE_CONFIDENCE = 0.85

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
    return RequirementResult(
        requirementId=requirement.requirement_id,
        status="met",
        score=1.0,
        confidence=JUDGE_CONFIDENCE,
        evidenceRefs=refs,
        evidenceSources={ref: present[ref].evidence_source for ref in refs},
        reasonCode=JUDGE_REASON_CODE,
        evidenceExplanation=f"CV có nội dung đáp ứng yêu cầu: {quotes}.",
        groupOperator=rule_result.group_operator,
    )


def _agree(rule_status: str, reading) -> bool | None:
    """Whether the two readings point the same way; ``None`` when there is nothing to compare."""

    if reading is None or reading.relation == "unsure":
        return None
    if reading.relation == "satisfies":
        return rule_status == "met"
    if reading.relation == "unrelated":
        return rule_status == "not_met"
    return rule_status == "unknown"
