"""Partial optimal transport for assigning evidence spans to requirements.

The retrieval step picks evidence for each requirement on its own. Nothing
stops one passage from being spent on every requirement at once, and nothing
stops a longer posting from collecting more credit than a short one: the
sparse score is a sum over the query's terms, so adding a requirement the
candidate cannot meet still moved the score, and repeating a skill in the CV
still moved it again.

Both are the same shortfall: there is no budget. A passage can only be
evidence for so much, and a requirement can only be answered so far. Stating
that as a transport problem makes the budget explicit - requirement masses on
one side, evidence masses on the other, and a cost for carrying one to the
other. The ``partial`` half matters because neither side must be spent in
full: a CV holds passages no posting asked for, and a posting holds
requirements no passage answers. Those are the masses left where they are,
not errors to minimise away.

This module solves that problem and nothing else. It decides no requirement
status, reads no document, and calls nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import exp, isfinite

# Entropic regularisation. Smaller is closer to the true transport plan and
# slower to converge; this value keeps a span from being split across many
# requirements while still converging in a few dozen iterations on the matrix
# sizes seen here (a posting's requirements against one CV's passages).
DEFAULT_REGULARISATION = 0.05

# What it costs to leave a unit of mass where it is. A pair costs
# ``1 - similarity``, so this is the cost of a pair at similarity 0.30 - the
# same floor retrieval uses. Below that a requirement would rather stay
# unanswered than claim the passage, which is the behaviour we want.
DEFAULT_UNMATCHED_COST = 0.70

DEFAULT_MAX_ITERATIONS = 200
DEFAULT_TOLERANCE = 1e-9

# The share of a passage a requirement must end up holding before it counts as
# holding it at all. A contested passage leaves the losing claims a fraction of
# a percent rather than a clean zero - that is the solver declining them, and
# reading those crumbs as a kept claim would report that nothing was taken back
# when in fact everything was.
MATERIAL_SHARE = 0.10


@dataclass(frozen=True)
class TransportPlan:
    """How much of each evidence span each requirement ends up holding."""

    plan: tuple[tuple[float, ...], ...]
    requirement_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    unmatched_requirement_mass: tuple[float, ...] = field(default=())
    unmatched_evidence_mass: tuple[float, ...] = field(default=())

    def mass_for(self, requirement_id: str) -> float:
        if requirement_id not in self.requirement_ids:
            return 0.0
        row = self.requirement_ids.index(requirement_id)
        return sum(self.plan[row])

    def holders_of(self, evidence_id: str, *, minimum: float = MATERIAL_SHARE) -> list[str]:
        """Requirements that kept a share of this span, strongest first."""
        if evidence_id not in self.evidence_ids:
            return []
        column = self.evidence_ids.index(evidence_id)
        held = [
            (self.plan[row][column], requirement_id)
            for row, requirement_id in enumerate(self.requirement_ids)
            if self.plan[row][column] > minimum
        ]
        return [requirement_id for _, requirement_id in sorted(held, key=lambda item: (-item[0], item[1]))]


def solve_partial_transport(
    cost: list[list[float]],
    supply: list[float],
    demand: list[float],
    *,
    regularisation: float = DEFAULT_REGULARISATION,
    unmatched_cost: float = DEFAULT_UNMATCHED_COST,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    tolerance: float = DEFAULT_TOLERANCE,
) -> list[list[float]]:
    """A transport plan that may leave mass on either side, under a hard budget.

    Relaxing both marginals with a KL penalty - the usual unbalanced form -
    lets a plan *exceed* them, and exceeding them is exactly what this is here
    to stop: one passage answering three requirements at once is the defect,
    not a rounding artefact. So the budget stays a hard constraint and the
    freedom to leave mass behind is bought differently: one extra row and one
    extra column absorb whatever goes unassigned, at ``unmatched_cost`` a unit.

    The extended problem is balanced, so plain Sinkhorn applies and its
    marginals are matched exactly. Every real row then sums to at most its
    supply and every real column to at most its demand, with the remainder
    parked in the extra row and column.

    The iteration count is bounded and the result is rounded, so the same input
    gives the same plan on every run - this feeds a cached, hashed result.
    """
    rows, columns = len(supply), len(demand)
    if rows == 0 or columns == 0:
        return [[0.0] * columns for _ in range(rows)]
    if any(len(row) != columns for row in cost) or len(cost) != rows:
        raise ValueError("cost matrix does not match the supply and demand sizes")

    total_supply, total_demand = sum(supply), sum(demand)
    extended_cost = [
        [*row, unmatched_cost] for row in cost
    ] + [[unmatched_cost] * columns + [0.0]]
    extended_supply = [*supply, total_demand]
    extended_demand = [*demand, total_supply]

    kernel = [[exp(-value / regularisation) for value in row] for row in extended_cost]
    left = [1.0] * (rows + 1)
    right = [1.0] * (columns + 1)
    for _ in range(max_iterations):
        previous = list(left)
        for i in range(rows + 1):
            total = sum(kernel[i][j] * right[j] for j in range(columns + 1))
            left[i] = extended_supply[i] / total if total > 0 else 0.0
        for j in range(columns + 1):
            total = sum(kernel[i][j] * left[i] for i in range(rows + 1))
            right[j] = extended_demand[j] / total if total > 0 else 0.0
        shift = max(abs(a - b) for a, b in zip(left, previous, strict=True))
        if shift < tolerance:
            break

    return [
        [
            round(left[i] * kernel[i][j] * right[j], 9)
            if isfinite(left[i] * right[j])
            else 0.0
            for j in range(columns)
        ]
        for i in range(rows)
    ]


def assign_evidence(
    requirement_ids: list[str],
    evidence_ids: list[str],
    similarity: dict[tuple[str, str], float],
    *,
    requirement_mass: dict[str, float] | None = None,
    evidence_capacity: float = 1.0,
    **solver_options: float | int,
) -> TransportPlan:
    """Assign evidence to requirements under a budget on both sides.

    ``similarity`` is the retrieval score already computed for a
    (requirement, evidence) pair, in [0, 1]; a pair absent from the mapping
    cannot be carried at all. ``evidence_capacity`` is what one passage is
    worth in total, which is the budget that stops a single passage from
    answering every requirement at once.
    """
    requirement_ids = list(dict.fromkeys(requirement_ids))
    evidence_ids = list(dict.fromkeys(evidence_ids))
    masses = requirement_mass or {}

    # An unreachable pair costs more than any reachable one rather than
    # infinity, so the kernel stays a number and the solver stays stable.
    cost = [
        [1.0 - min(1.0, max(0.0, similarity.get((requirement, evidence), 0.0))) + (
            0.0 if (requirement, evidence) in similarity else 10.0
        ) for evidence in evidence_ids]
        for requirement in requirement_ids
    ]
    supply = [max(0.0, masses.get(requirement, 1.0)) for requirement in requirement_ids]
    demand = [evidence_capacity] * len(evidence_ids)

    plan = solve_partial_transport(cost, supply, demand, **solver_options)
    return TransportPlan(
        plan=tuple(tuple(row) for row in plan),
        requirement_ids=tuple(requirement_ids),
        evidence_ids=tuple(evidence_ids),
        unmatched_requirement_mass=tuple(
            round(max(0.0, supply[i] - sum(plan[i])), 9) for i in range(len(requirement_ids))
        ),
        unmatched_evidence_mass=tuple(
            round(
                max(0.0, demand[j] - sum(plan[i][j] for i in range(len(requirement_ids)))), 9
            )
            for j in range(len(evidence_ids))
        ),
    )


def duplicate_claims_removed(
    similarity: dict[tuple[str, str], float],
    assignment: TransportPlan,
    *,
    threshold: float = 0.30,
    minimum_mass: float = MATERIAL_SHARE,
) -> int:
    """How many (requirement, evidence) claims the budget took back.

    Retrieval would hand a passage to every requirement whose score clears the
    threshold. The transport plan keeps only what the passage can actually
    support - this counts the difference, which is the number that says whether
    the budget did any work on a given pair.
    """
    claimed = sum(1 for value in similarity.values() if value >= threshold)
    kept = sum(
        1
        for row in assignment.plan
        for value in row
        if value > minimum_mass
    )
    return max(0, claimed - kept)
