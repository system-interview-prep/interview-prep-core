"""The score answers "which needs are met", not "how long is the posting".

The last two classes are the acceptance criteria written down at the end of
Phase 1 for this change, stated at the level the score is actually computed.
"""

import pytest

from src.modules.matching.evaluation.coverage_semantic import (
    ScoredRequirement,
    ScoredSpan,
    deduplicate_spans,
    score_by_coverage,
    shares_enough_wording,
)


def requirement(requirement_id: str, text: str, priority: str = "must_have"):
    return ScoredRequirement(requirement_id=requirement_id, priority=priority, text=text)


def span(evidence_id: str, text: str):
    return ScoredSpan(evidence_id=evidence_id, text=text)


def keyword_similarity(left: str, right: str) -> float:
    """A stand-in scorer: share of the requirement's words the span contains."""
    wanted = [word for word in left.casefold().split() if len(word) > 2]
    if not wanted:
        return 0.0
    have = set(right.casefold().split())
    return sum(1 for word in wanted if word in have) / len(wanted)


def score(requirements, spans, **kwargs):
    return score_by_coverage(
        requirements, spans, sparse_similarity=keyword_similarity, **kwargs
    )


class TestNothingToScore:
    def test_no_requirement_scores_nothing(self):
        assert score([], [span("ev-1", "java backend")]) is None

    def test_no_evidence_scores_nothing(self):
        assert score([requirement("req-1", "java backend")], []) is None

    def test_a_blank_requirement_is_skipped(self):
        assert score([requirement("req-1", "   ")], [span("ev-1", "java")]) is None


class TestCoverage:
    def test_an_answered_requirement_scores_near_full(self):
        result = score(
            [requirement("req-java", "java backend service")],
            [span("ev-1", "built a java backend service at acme")],
        )
        assert result.score > 0.9

    def test_an_unanswered_requirement_scores_near_zero(self):
        result = score(
            [requirement("req-rust", "rust systems programming")],
            [span("ev-1", "taught english literature")],
        )
        assert result.score < 0.05

    def test_half_the_needs_answered_scores_about_half(self):
        result = score(
            [
                requirement("req-java", "java backend service"),
                requirement("req-rust", "rust systems programming"),
            ],
            [span("ev-1", "built a java backend service at acme")],
        )
        assert 0.35 < result.score < 0.65

    def test_a_nice_to_have_counts_for_less_than_a_must_have(self):
        """Same evidence, same posting size: which need it answers decides."""
        evidence = [span("ev-1", "built a java backend service at acme")]
        must_answered = score(
            [
                requirement("req-a", "java backend service", "must_have"),
                requirement("req-b", "rust systems programming", "nice_to_have"),
            ],
            evidence,
        )
        nice_answered = score(
            [
                requirement("req-a", "rust systems programming", "must_have"),
                requirement("req-b", "java backend service", "nice_to_have"),
            ],
            evidence,
        )
        assert must_answered.score > nice_answered.score
        # 1.00 of 1.35 demand against 0.35 of 1.35.
        assert must_answered.score == pytest.approx(1.0 / 1.35, abs=0.02)
        assert nice_answered.score == pytest.approx(0.35 / 1.35, abs=0.02)

    def test_answered_requirements_are_reported(self):
        result = score(
            [
                requirement("req-java", "java backend service"),
                requirement("req-rust", "rust systems programming"),
            ],
            [span("ev-1", "built a java backend service at acme")],
        )
        assert result.answered_requirement_ids == ("req-java",)


class TestMr2AddingAnUnmeetableRequirement:
    """Phase 1, violation 2: the score moved by +0.10 to +0.78 for this."""

    def test_the_score_does_not_rise(self):
        spans = [span("ev-1", "built a java backend service at acme")]
        before = score([requirement("req-java", "java backend service")], spans)
        after = score(
            [
                requirement("req-java", "java backend service"),
                requirement("req-impossible", "piloting a commercial airliner"),
            ],
            spans,
        )
        assert after.score <= before.score + 1e-9

    def test_the_score_falls_because_the_posting_asks_for_more(self):
        spans = [span("ev-1", "built a java backend service at acme")]
        before = score([requirement("req-java", "java backend service")], spans)
        after = score(
            [
                requirement("req-java", "java backend service"),
                requirement("req-impossible", "piloting a commercial airliner"),
            ],
            spans,
        )
        assert after.score < before.score

    def test_the_answered_requirement_keeps_its_evidence(self):
        spans = [span("ev-1", "built a java backend service at acme")]
        after = score(
            [
                requirement("req-java", "java backend service"),
                requirement("req-impossible", "piloting a commercial airliner"),
            ],
            spans,
        )
        assert "req-java" in after.answered_requirement_ids


class TestMr3RepeatingAPassage:
    """Phase 1, violation 3: duplicating a skill nudged the score upward."""

    def test_an_identical_passage_is_one_piece_of_evidence(self):
        spans = [span("ev-1", "built a java backend service"), span("ev-2", "built a java backend service")]
        assert len(deduplicate_spans(spans)) == 1

    def test_case_and_spacing_do_not_make_a_second_passage(self):
        spans = [span("ev-1", "Built a  Java backend"), span("ev-2", "built a java backend")]
        assert len(deduplicate_spans(spans)) == 1

    def test_duplicating_a_passage_does_not_change_the_score(self):
        requirements = [
            requirement("req-java", "java backend service"),
            requirement("req-api", "java backend service"),
        ]
        single = score(requirements, [span("ev-1", "built a java backend service")])
        doubled = score(
            requirements,
            [
                span("ev-1", "built a java backend service"),
                span("ev-2", "built a java backend service"),
            ],
        )
        assert doubled.score == pytest.approx(single.score, abs=1e-9)

    def test_a_genuinely_different_passage_still_counts(self):
        requirements = [
            requirement("req-java", "java backend service"),
            requirement("req-sql", "postgresql query tuning"),
        ]
        one = score(requirements, [span("ev-1", "built a java backend service")])
        two = score(
            requirements,
            [
                span("ev-1", "built a java backend service"),
                span("ev-2", "postgresql query tuning at scale"),
            ],
        )
        assert two.score > one.score


class TestHybridHalves:
    def test_the_dense_half_participates(self):
        spans = [span("ev-1", "unrelated wording entirely")]
        sparse_only = score_by_coverage(
            [requirement("req-1", "java backend")],
            spans,
            sparse_similarity=keyword_similarity,
        )
        with_dense = score_by_coverage(
            [requirement("req-1", "java backend")],
            spans,
            sparse_similarity=keyword_similarity,
            dense_similarity=lambda a, b: 0.95,
        )
        assert sparse_only is not None and sparse_only.score < 0.05
        assert with_dense.score > 0.4

    def test_the_weight_between_halves_is_respected(self):
        spans = [span("ev-1", "unrelated wording entirely")]
        heavy_dense = score_by_coverage(
            [requirement("req-1", "java backend")],
            spans,
            sparse_similarity=keyword_similarity,
            dense_similarity=lambda a, b: 0.9,
            bm25_weight=0.0,
        )
        heavy_sparse = score_by_coverage(
            [requirement("req-1", "java backend")],
            spans,
            sparse_similarity=keyword_similarity,
            dense_similarity=lambda a, b: 0.9,
            bm25_weight=0.9,
        )
        assert heavy_dense.score > (heavy_sparse.score if heavy_sparse else 0.0)


class TestSyllableCollisionGuard:
    """A floor on similarity is not a guard when syllables collide.

    Measured on the corpus: the probe below drew real mass from an ordinary CV
    and raised the pair's score, which is the same collision the requirement
    evaluator already answers with a strict majority of the claim's syllables.
    """

    PROBE = "Kinh nghiem van hanh lo phan ung hat nhan"

    def test_a_degree_line_does_not_answer_a_nuclear_reactor_requirement(self):
        assert not shares_enough_wording(
            self.PROBE,
            "Cử nhân ngành Hệ thống Thông tin - Trường Đại học Công nghệ Thông tin",
        )

    def test_text_that_really_is_about_it_still_answers(self):
        assert shares_enough_wording(
            self.PROBE, "Vận hành lò phản ứng hạt nhân tại nhà máy, kinh nghiệm 5 năm"
        )

    def test_the_bar_scales_with_the_size_of_the_claim(self):
        """Two hits carried a thirteen-syllable sentence under a flat floor."""
        long_claim = "van hanh he thong giam sat phong xa trong lo phan ung hat nhan"
        assert not shares_enough_wording(long_claim, "he thong giam sat")
        assert shares_enough_wording("he thong giam sat", "he thong giam sat nhiet do")

    def test_the_dense_half_is_not_blocked_by_the_guard(self):
        """Different words for the same thing is what the dense half is for."""
        result = score_by_coverage(
            [requirement("req-1", "java backend service")],
            [span("ev-1", "wrote server side software in a jvm language")],
            sparse_similarity=keyword_similarity,
            dense_similarity=lambda a, b: 0.9,
        )
        assert result is not None and result.score > 0.4
