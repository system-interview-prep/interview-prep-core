"""Expected information gain for choosing which clarification to ask.

The clarification gate currently decides *whether* to ask about an unknown
requirement from a fixed confidence threshold, one LLM call per unknown, in
requirement-id order.  A threshold cannot see whether the answer could change
anything: a must-have that another requirement has already disqualified scores
the same as the one requirement still standing between ``review_required`` and
``eligible``.

This module ranks unknown requirements by the expected reduction in entropy of
the decision variable instead (Lindley).  It is deterministic, calls nothing,
and decides no requirement status - callers keep the existing gate for the
requirements this module puts first.

The decision variable mirrors ``MatchingFacade._eligibility`` exactly:

    ineligible       if any must-have is ``not_met`` with a conclusive reason
    review_required  if any must-have is still ``unknown``
    eligible         otherwise

Two consequences drive the ranking, and neither depends on the calibration
below:

* In a pair that is already ``ineligible``, no answer can move the decision.
  Every gain is 0 and no question is worth an LLM call.
* A requirement that is not a must-have cannot move the decision either, so it
  scores 0 even when its evidence is weak enough to look urgent to a threshold.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import log2

# Probability that a requirement turns out ``met`` once the candidate supplies
# the missing detail, keyed by the reason the evaluator left it ``unknown``.
#
# These are calibration constants, not measured posteriors.  Where the
# evaluator's own ``confidence`` expresses evidence strength toward the
# requirement, that value is reused; where it expresses confidence in the
# *unknown verdict itself* (``skill_duration_not_evidenced`` carries 1.0 and
# means "the duration is certainly not stated", not "the skill is certainly
# present"), it is replaced by the neutral 0.5 rather than read as a
# probability it never was.  Ranking inside a pair depends on these values;
# whether a question is worth asking at all does not.
_MET_PROBABILITY_BY_REASON: dict[str, float] = {
    "generic_requirement_evidence_weak": 0.35,
    "skill_retrieved_evidence_needs_confirmation": 0.45,
    "experience_duration_ambiguous": 0.45,
    "skill_semantic_context_needs_confirmation": 0.55,
    "concept_group_evidence_missing": 0.60,
    "skill_duration_not_evidenced": 0.50,
}
_DEFAULT_MET_PROBABILITY = 0.50

# Probabilities are clamped away from 0 and 1: a requirement certain to be met
# carries no information, and one certain to fail cannot be rescued by asking.
_MIN_PROBABILITY = 0.01
_MAX_PROBABILITY = 0.99


@dataclass(frozen=True)
class UnknownRequirement:
    """One requirement left ``unknown``, as the ranking sees it."""

    requirement_id: str
    is_must_have: bool
    met_probability: float


@dataclass(frozen=True)
class RankedQuestion:
    requirement_id: str
    information_gain: float
    met_probability: float


def met_probability_for(reason_code: str | None, confidence: float | None = None) -> float:
    """Calibrated probability that the requirement is met once answered.

    ``confidence`` is accepted so callers can pass the evaluator's value for
    reason codes this table does not know yet; it is used only as a fallback
    and is clamped like every other probability.
    """

    if reason_code in _MET_PROBABILITY_BY_REASON:
        return _MET_PROBABILITY_BY_REASON[reason_code]
    if confidence is not None and 0.0 < confidence < 1.0:
        return _clamp(float(confidence))
    return _DEFAULT_MET_PROBABILITY


def binary_entropy(probability: float) -> float:
    """Entropy in bits of a Bernoulli variable, 0 at both certain ends."""

    if probability <= 0.0 or probability >= 1.0:
        return 0.0
    return -(
        probability * log2(probability) + (1.0 - probability) * log2(1.0 - probability)
    )


def decision_entropy(
    unknowns: list[UnknownRequirement],
    *,
    already_disqualified: bool,
) -> float:
    """Entropy of the final decision given what is still unknown.

    Once every unknown must-have is answered the decision is ``eligible`` only
    if all of them come back ``met``; any single ``not_met`` makes it
    ``ineligible``.  Under independence the decision is therefore Bernoulli
    with parameter equal to the product of the per-requirement probabilities.
    """

    if already_disqualified:
        return 0.0
    return binary_entropy(_eligible_probability(unknowns))


def expected_information_gain(
    requirement_id: str,
    unknowns: list[UnknownRequirement],
    *,
    already_disqualified: bool,
) -> float:
    """Expected bits of decision entropy removed by asking about one requirement.

    Answering ``not_met`` settles the decision outright, which is why only the
    ``met`` branch carries residual entropy.
    """

    if already_disqualified:
        return 0.0
    target = next(
        (item for item in unknowns if item.requirement_id == requirement_id), None
    )
    if target is None or not target.is_must_have:
        return 0.0

    probability = _clamp(target.met_probability)
    prior = _eligible_probability(unknowns)
    # The others' product: the posterior after a ``met`` answer.
    posterior_if_met = _eligible_probability(
        [item for item in unknowns if item.requirement_id != requirement_id]
    )
    gain = binary_entropy(prior) - probability * binary_entropy(posterior_if_met)
    return max(0.0, round(gain, 6))


def rank_by_information_gain(
    unknowns: list[UnknownRequirement],
    *,
    already_disqualified: bool,
    minimum_gain: float = 0.0,
) -> list[RankedQuestion]:
    """Order the unknown requirements by how much the answer would settle.

    Requirements whose gain does not exceed ``minimum_gain`` are dropped: for
    the default they are the ones that provably cannot move the decision, so
    asking about them spends an LLM call on a question with no consequence.
    Ties break on requirement id, so the order is stable across runs.
    """

    ranked = [
        RankedQuestion(
            requirement_id=item.requirement_id,
            information_gain=expected_information_gain(
                item.requirement_id, unknowns, already_disqualified=already_disqualified
            ),
            met_probability=_clamp(item.met_probability),
        )
        for item in unknowns
    ]
    return sorted(
        (item for item in ranked if item.information_gain > minimum_gain),
        key=lambda item: (-item.information_gain, item.requirement_id),
    )


def _eligible_probability(unknowns: list[UnknownRequirement]) -> float:
    probability = 1.0
    for item in unknowns:
        if item.is_must_have:
            probability *= _clamp(item.met_probability)
    return probability


def _clamp(probability: float) -> float:
    return min(_MAX_PROBABILITY, max(_MIN_PROBABILITY, probability))
