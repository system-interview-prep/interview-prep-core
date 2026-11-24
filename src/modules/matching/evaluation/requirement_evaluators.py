"""Entry point for evaluating one unresolved requirement against one CV.

Two paths meet here.  ``legacy_rules`` is the phrase-table evaluator the
matcher has always used; it still runs for every requirement and is the only
path that can produce ``not_met``.  ``judge_evaluators`` combines that verdict
with the judge's grounded reading when a ``ReadingSet`` was built for this
match (``Settings.matching_judge_enabled``), and can raise it, never lower it.

Callers that pass no ``readings`` - the targeted re-parse, the golden runner,
every existing test - get the rule verdict unchanged.
"""

from __future__ import annotations

from src.modules.matching.domain.schemas import RequirementResult, UnresolvedRequirement
from src.modules.matching.evaluation.judge_evaluators import (
    evaluate_with_numeric,
    evaluate_with_readings,
)
from src.modules.matching.evaluation.legacy_rules import EvaluatorSelection, select_evaluator
from src.modules.matching.evaluation.legacy_rules import (
    evaluate_unresolved_requirement as evaluate_by_rules,
)
from src.modules.matching.judge.readings import ReadingSet
from src.modules.user_cvs.schemas import CanonicalResume

__all__ = [
    "EvaluatorSelection",
    "evaluate_by_rules",
    "evaluate_unresolved_requirement",
    "select_evaluator",
]


def evaluate_unresolved_requirement(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
    readings: ReadingSet | None = None,
) -> RequirementResult:
    result = evaluate_by_rules(requirement, resume)
    if readings is None:
        return result
    selection = select_evaluator(requirement)
    if selection.name in ("gpa", "language_credential"):
        return evaluate_with_numeric(requirement, resume, readings, result)
    return evaluate_with_readings(requirement, resume, readings, result, selection)
