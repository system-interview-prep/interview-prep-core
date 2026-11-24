"""Expected information gain ranks clarifications by effect on the decision."""

import pytest

from src.modules.matching.clarifications.information_gain import (
    UnknownRequirement,
    binary_entropy,
    decision_entropy,
    expected_information_gain,
    met_probability_for,
    rank_by_information_gain,
)


def unknown(requirement_id: str, probability: float, *, must_have: bool = True):
    return UnknownRequirement(
        requirement_id=requirement_id,
        is_must_have=must_have,
        met_probability=probability,
    )


class TestBinaryEntropy:
    def test_certainty_carries_no_entropy(self):
        assert binary_entropy(0.0) == 0.0
        assert binary_entropy(1.0) == 0.0

    def test_even_odds_carry_one_bit(self):
        assert binary_entropy(0.5) == pytest.approx(1.0)

    def test_entropy_is_symmetric(self):
        assert binary_entropy(0.3) == pytest.approx(binary_entropy(0.7))


class TestDecisionEntropy:
    def test_a_disqualified_pair_has_nothing_left_to_learn(self):
        unknowns = [unknown("req-1", 0.5), unknown("req-2", 0.5)]
        assert decision_entropy(unknowns, already_disqualified=True) == 0.0

    def test_unknown_must_haves_compound(self):
        one = decision_entropy([unknown("req-1", 0.5)], already_disqualified=False)
        two = decision_entropy(
            [unknown("req-1", 0.5), unknown("req-2", 0.5)], already_disqualified=False
        )
        # Two coin flips make ``eligible`` less likely (0.25), so the decision
        # is more predictable, not less.
        assert one == pytest.approx(1.0)
        assert two == pytest.approx(binary_entropy(0.25))
        assert two < one

    def test_requirements_that_cannot_disqualify_are_ignored(self):
        with_nice_to_have = decision_entropy(
            [unknown("req-1", 0.5), unknown("req-2", 0.5, must_have=False)],
            already_disqualified=False,
        )
        assert with_nice_to_have == pytest.approx(
            decision_entropy([unknown("req-1", 0.5)], already_disqualified=False)
        )


class TestExpectedInformationGain:
    def test_no_question_helps_once_the_pair_is_disqualified(self):
        """The paper's first case: a must-have already denied by other evidence."""
        unknowns = [unknown("req-1", 0.5), unknown("req-2", 0.4)]
        for item in unknowns:
            assert (
                expected_information_gain(
                    item.requirement_id, unknowns, already_disqualified=True
                )
                == 0.0
            )

    def test_the_only_remaining_blocker_settles_the_decision(self):
        """The paper's second case: one requirement stands between two outcomes."""
        unknowns = [unknown("req-1", 0.5)]
        gain = expected_information_gain(
            "req-1", unknowns, already_disqualified=False
        )
        # Answering it leaves no uncertainty at all, so the full prior entropy
        # of the decision is removed.
        assert gain == pytest.approx(1.0)

    def test_a_nice_to_have_cannot_move_the_decision(self):
        unknowns = [unknown("req-1", 0.5), unknown("req-2", 0.3, must_have=False)]
        assert (
            expected_information_gain("req-2", unknowns, already_disqualified=False)
            == 0.0
        )

    def test_a_requirement_certain_to_be_met_teaches_nothing(self):
        unknowns = [unknown("req-1", 0.99), unknown("req-2", 0.5)]
        certain = expected_information_gain(
            "req-1", unknowns, already_disqualified=False
        )
        uncertain = expected_information_gain(
            "req-2", unknowns, already_disqualified=False
        )
        assert certain < uncertain

    def test_an_absent_requirement_scores_nothing(self):
        assert (
            expected_information_gain(
                "req-absent", [unknown("req-1", 0.5)], already_disqualified=False
            )
            == 0.0
        )

    def test_gain_is_never_negative(self):
        unknowns = [unknown(f"req-{i}", p) for i, p in enumerate([0.1, 0.35, 0.9])]
        for item in unknowns:
            assert (
                expected_information_gain(
                    item.requirement_id, unknowns, already_disqualified=False
                )
                >= 0.0
            )


class TestRanking:
    def test_a_disqualified_pair_yields_no_questions(self):
        unknowns = [unknown("req-1", 0.45), unknown("req-2", 0.35)]
        assert rank_by_information_gain(unknowns, already_disqualified=True) == []

    def test_questions_that_cannot_change_anything_are_dropped(self):
        unknowns = [
            unknown("req-must", 0.5),
            unknown("req-nice", 0.35, must_have=False),
        ]
        ranked = rank_by_information_gain(unknowns, already_disqualified=False)
        assert [item.requirement_id for item in ranked] == ["req-must"]

    def test_order_is_stable_for_equal_gains(self):
        unknowns = [unknown("req-b", 0.5), unknown("req-a", 0.5)]
        ranked = rank_by_information_gain(unknowns, already_disqualified=False)
        assert [item.requirement_id for item in ranked] == ["req-a", "req-b"]

    def test_ranking_does_not_depend_on_input_order(self):
        unknowns = [unknown("req-1", 0.2), unknown("req-2", 0.6), unknown("req-3", 0.5)]
        forward = rank_by_information_gain(unknowns, already_disqualified=False)
        backward = rank_by_information_gain(
            list(reversed(unknowns)), already_disqualified=False
        )
        assert [item.requirement_id for item in forward] == [
            item.requirement_id for item in backward
        ]


class TestCalibration:
    def test_known_reason_codes_use_the_calibration_table(self):
        assert met_probability_for("generic_requirement_evidence_weak") == 0.35
        assert met_probability_for("skill_retrieved_evidence_needs_confirmation") == 0.45

    def test_confidence_in_the_unknown_verdict_is_not_read_as_a_probability(self):
        """``skill_duration_not_evidenced`` carries confidence 1.0 in the corpus."""
        assert met_probability_for("skill_duration_not_evidenced", confidence=1.0) == 0.50

    def test_an_unseen_reason_code_falls_back_to_its_confidence(self):
        assert met_probability_for("some_new_reason_code", confidence=0.4) == 0.4

    def test_an_unseen_reason_code_without_confidence_is_neutral(self):
        assert met_probability_for(None) == 0.50
