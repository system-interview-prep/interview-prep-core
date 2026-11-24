"""A budget on both sides: one passage cannot answer every requirement.

The last three classes encode the acceptance criteria written down at the end
of Phase 1 for the aggregation change: a requirement nobody can meet must not
move another requirement's evidence (MR2), and repeating a passage must not
buy more credit than the passage is worth (MR3).
"""

import pytest

from src.modules.matching.retrieval.partial_transport import (
    assign_evidence,
    duplicate_claims_removed,
    solve_partial_transport,
)


class TestSolver:
    def test_an_empty_side_transports_nothing(self):
        assert solve_partial_transport([], [], [1.0]) == []
        assert solve_partial_transport([[]], [1.0], []) == [[]]

    def test_a_mismatched_cost_matrix_is_rejected(self):
        with pytest.raises(ValueError):
            solve_partial_transport([[0.1]], [1.0], [1.0, 1.0])

    def test_mass_goes_where_the_cost_is_lowest(self):
        plan = solve_partial_transport([[0.0, 1.0]], [1.0], [1.0, 1.0])
        assert plan[0][0] > plan[0][1]

    def test_the_plan_never_exceeds_what_each_side_holds(self):
        plan = solve_partial_transport([[0.0, 0.0], [0.0, 0.0]], [1.0, 1.0], [1.0, 1.0])
        for row, supply in zip(plan, [1.0, 1.0], strict=True):
            assert sum(row) <= supply + 1e-6
        for column in range(2):
            assert sum(row[column] for row in plan) <= 1.0 + 1e-6

    def test_the_same_input_gives_the_same_plan(self):
        cost = [[0.2, 0.7], [0.6, 0.1]]
        first = solve_partial_transport(cost, [1.0, 1.0], [1.0, 1.0])
        second = solve_partial_transport(cost, [1.0, 1.0], [1.0, 1.0])
        assert first == second


class TestAssignment:
    def test_a_requirement_takes_the_passage_that_answers_it(self):
        assignment = assign_evidence(
            ["req-java"],
            ["ev-java", "ev-cooking"],
            {("req-java", "ev-java"): 0.9, ("req-java", "ev-cooking"): 0.1},
        )
        held = assignment.plan[0]
        assert held[0] > held[1]

    def test_evidence_nobody_asked_about_stays_unmatched(self):
        assignment = assign_evidence(
            ["req-java"],
            ["ev-java", "ev-unrelated"],
            {("req-java", "ev-java"): 0.95},
        )
        column = assignment.evidence_ids.index("ev-unrelated")
        assert assignment.unmatched_evidence_mass[column] > 0.9

    def test_a_requirement_with_no_evidence_keeps_its_mass(self):
        assignment = assign_evidence(
            ["req-java", "req-rust"],
            ["ev-java"],
            {("req-java", "ev-java"): 0.95},
        )
        row = assignment.requirement_ids.index("req-rust")
        assert assignment.unmatched_requirement_mass[row] > 0.9

    def test_one_passage_cannot_fully_answer_two_requirements(self):
        """The budget: a span is worth one unit no matter who wants it."""
        assignment = assign_evidence(
            ["req-a", "req-b"],
            ["ev-shared"],
            {("req-a", "ev-shared"): 0.9, ("req-b", "ev-shared"): 0.9},
        )
        total = sum(row[0] for row in assignment.plan)
        assert total <= 1.0 + 1e-6
        assert len(assignment.holders_of("ev-shared")) >= 1

    def test_the_stronger_claim_keeps_more_of_a_contested_passage(self):
        assignment = assign_evidence(
            ["req-weak", "req-strong"],
            ["ev-shared"],
            {("req-weak", "ev-shared"): 0.35, ("req-strong", "ev-shared"): 0.95},
        )
        assert assignment.holders_of("ev-shared")[0] == "req-strong"

    def test_the_order_of_the_inputs_does_not_change_the_result(self):
        similarity = {
            ("req-a", "ev-1"): 0.8,
            ("req-b", "ev-2"): 0.7,
            ("req-a", "ev-2"): 0.2,
        }
        forward = assign_evidence(["req-a", "req-b"], ["ev-1", "ev-2"], similarity)
        backward = assign_evidence(["req-b", "req-a"], ["ev-2", "ev-1"], similarity)
        assert forward.mass_for("req-a") == pytest.approx(backward.mass_for("req-a"), abs=1e-6)
        assert forward.mass_for("req-b") == pytest.approx(backward.mass_for("req-b"), abs=1e-6)


class TestMr2AddingAnUnmeetableRequirement:
    """Phase 1, violation 2: a requirement nobody can meet moved the score."""

    def test_it_does_not_touch_what_the_others_hold(self):
        similarity = {("req-java", "ev-java"): 0.9, ("req-sql", "ev-sql"): 0.8}
        before = assign_evidence(["req-java", "req-sql"], ["ev-java", "ev-sql"], similarity)
        after = assign_evidence(
            ["req-java", "req-sql", "req-impossible"],
            ["ev-java", "ev-sql"],
            similarity,  # the new requirement matches nothing
        )
        assert after.mass_for("req-java") == pytest.approx(before.mass_for("req-java"), abs=1e-6)
        assert after.mass_for("req-sql") == pytest.approx(before.mass_for("req-sql"), abs=1e-6)

    def test_the_unmeetable_requirement_takes_nothing(self):
        assignment = assign_evidence(
            ["req-java", "req-impossible"],
            ["ev-java"],
            {("req-java", "ev-java"): 0.9},
        )
        assert assignment.mass_for("req-impossible") < 1e-3


class TestMr3RepeatingASkill:
    """Phase 1, violation 3: duplicating a skill nudged the score upward."""

    def test_a_duplicated_passage_does_not_double_the_credit(self):
        single = assign_evidence(
            ["req-java"], ["ev-java"], {("req-java", "ev-java"): 0.9}
        )
        duplicated = assign_evidence(
            ["req-java"],
            ["ev-java", "ev-java-copy"],
            {("req-java", "ev-java"): 0.9, ("req-java", "ev-java-copy"): 0.9},
        )
        # The requirement's own mass is the ceiling, so a second copy of the
        # same passage cannot lift it past what one requirement is worth.
        assert duplicated.mass_for("req-java") <= 1.0 + 1e-6
        assert duplicated.mass_for("req-java") >= single.mass_for("req-java") - 1e-6


class TestDuplicateClaimsRemoved:
    def test_nothing_is_taken_back_when_each_passage_has_one_claimant(self):
        similarity = {("req-a", "ev-1"): 0.9, ("req-b", "ev-2"): 0.9}
        assignment = assign_evidence(["req-a", "req-b"], ["ev-1", "ev-2"], similarity)
        assert duplicate_claims_removed(similarity, assignment) == 0

    def test_a_contested_passage_costs_the_weaker_claim(self):
        similarity = {
            ("req-a", "ev-shared"): 0.95,
            ("req-b", "ev-shared"): 0.40,
            ("req-c", "ev-shared"): 0.35,
        }
        assignment = assign_evidence(
            ["req-a", "req-b", "req-c"], ["ev-shared"], similarity
        )
        assert duplicate_claims_removed(similarity, assignment) >= 1
