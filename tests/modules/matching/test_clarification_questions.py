from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.modules.matching.clarifications.question_generation import (
    ClarificationPlan,
    ClarificationQuestionService,
    build_template_question,
    EmbeddingSemanticScorer,
    GeneratedQuestion,
    OpenAIQuestionGenerator,
    build_clarification_question_service_from_env,
)


# What the plan is owed when no model draft survives: the duration template
# with the plan's own subject term, and nothing the plan did not already carry.
_FLOOR_QUESTION = "Bạn đã trực tiếp sử dụng React trong khoảng thời gian bao lâu?"


def _plan(**updates) -> ClarificationPlan:
    data = {
        "requirementId": "req-react-duration",
        "missingDimension": "duration",
        "confidence": 0.91,
        "evidenceRefs": ["cv-ev-1"],
        "requirementText": "At least 24 months of React experience",
        "subjectTerms": ["React"],
        "evidenceTexts": ["Used React while building a customer portal."],
    }
    data.update(updates)
    return ClarificationPlan.model_validate(data)


class FakeGenerator:
    def __init__(self, question=None):
        self.question = question or GeneratedQuestion(
            questionText="Bạn đã trực tiếp sử dụng React trong khoảng thời gian bao lâu?",
            evidenceRefs=["cv-ev-1"],
        )
        self.plans = []

    def generate(self, plan):
        self.plans.append(plan)
        return self.question


class FakeScorer:
    def __init__(self, value=0.9, error=None):
        self.value = value
        self.error = error
        self.calls = []

    def score(self, question, intent):
        self.calls.append((question, intent))
        if self.error:
            raise self.error
        return self.value


def _service(generator=None, scorer=None, threshold=0.8):
    return ClarificationQuestionService(
        generator or FakeGenerator(), scorer or FakeScorer(), semantic_threshold=threshold
    )


def test_creates_ai_question_with_semantic_alignment_and_provenance() -> None:
    scorer = FakeScorer(0.88)
    result = _service(scorer=scorer).create(_plan())

    assert result is not None
    assert result.question_text == "Bạn đã trực tiếp sử dụng React trong khoảng thời gian bao lâu?"
    assert result.requirement_id == "req-react-duration"
    assert result.evidence_refs == ["cv-ev-1"]
    assert result.semantic_alignment_score == 0.88
    assert "bao lâu" in scorer.calls[0][1]


@pytest.mark.parametrize(
    "question",
    [
        "Bạn đã trực tiếp sử dụng React trong khoảng thời gian bao lâu? Hãy nêu ví dụ?",
        "Bạn đã trực tiếp sử dụng React trong khoảng thời gian bao lâu",
        "Bạn đã trực tiếp sử dụng React trong khoảng thời gian bao lâu?\nThêm câu hỏi?",
        "Bạn đã có 5 năm kinh nghiệm React chưa?",
        "Bạn có đáp ứng yêu cầu 24 tháng kinh nghiệm React không?",
        "Bạn có thể mô tả kinh nghiệm AWS của mình không?",
    ],
)
def test_replaces_an_unusable_draft_instead_of_asking_nothing(question) -> None:
    """The draft is still refused; the requirement is still asked about.

    Each case here would put a number, a tool or a second question in front of
    the candidate that the plan never supported. None of them reaches them.
    What changes is that refusing the draft no longer refuses the question: the
    requirement is unknown either way, and a template built from the plan asks
    for the missing fact without inventing anything.
    """

    result = _service(generator=FakeGenerator(GeneratedQuestion(questionText=question))).create(_plan())

    assert result is not None
    assert result.question_text != question
    assert result.question_text == _FLOOR_QUESTION
    assert "5" not in result.question_text
    assert "24" not in result.question_text
    assert "AWS" not in result.question_text


def test_allows_number_that_is_supported_by_source_text() -> None:
    plan = _plan(evidenceTexts=["Worked with React for 5 years on a customer portal."])
    question = GeneratedQuestion(questionText="Bạn đã dùng React trong 5 năm ở dự án nào?")
    result = _service(generator=FakeGenerator(question)).create(plan)
    assert result is not None


def test_rejects_evidence_reference_outside_supplied_cv_evidence() -> None:
    question = GeneratedQuestion(
        questionText="Bạn đã trực tiếp sử dụng React trong khoảng thời gian bao lâu?",
        evidenceRefs=["made-up-evidence"],
    )
    result = _service(generator=FakeGenerator(question)).create(_plan())

    assert result is not None
    assert result.evidence_refs == ["cv-ev-1"]
    assert "made-up-evidence" not in result.evidence_refs


def test_rejects_question_that_drops_required_subject() -> None:
    question = GeneratedQuestion(
        questionText="Bạn đã trực tiếp sử dụng kỹ năng này trong khoảng thời gian bao lâu?"
    )
    result = _service(generator=FakeGenerator(question)).create(_plan())

    assert result is not None
    assert "React" in result.question_text


def test_rejects_semantically_unrelated_question() -> None:
    scorer = FakeScorer(0.79)
    result = _service(scorer=scorer, threshold=0.8).create(_plan())

    # A score of 1.0 is the template's signature: it restates the intent, so it
    # cannot be the 0.79 draft that was just refused.
    assert result is not None
    assert result.semantic_alignment_score == 1.0


def test_retries_question_generation_after_semantic_rejection() -> None:
    class SequenceScorer(FakeScorer):
        def __init__(self):
            super().__init__(0.79)
            self.values = iter([0.79, 0.91])

        def score(self, question, intent):
            self.calls.append((question, intent))
            return next(self.values)

    generator = FakeGenerator()
    scorer = SequenceScorer()
    result = _service(generator=generator, scorer=scorer, threshold=0.8).create(_plan())

    assert result is not None
    assert result.semantic_alignment_score == 0.91
    assert len(generator.plans) == 2
    assert len(scorer.calls) == 2


def test_semantic_validation_error_still_asks_the_requirement() -> None:
    scorer = FakeScorer(error=RuntimeError("embedding unavailable"))
    result = _service(scorer=scorer).create(_plan())

    assert result is not None
    assert result.question_text == _FLOOR_QUESTION


def test_invalid_semantic_score_still_asks_the_requirement() -> None:
    result = _service(scorer=FakeScorer(1.2)).create(_plan())

    assert result is not None
    assert result.question_text == _FLOOR_QUESTION


def test_generator_failure_asks_without_calling_semantic_scorer() -> None:
    class FailedGenerator:
        def generate(self, plan):
            return None

    scorer = FakeScorer()
    result = _service(generator=FailedGenerator(), scorer=scorer).create(_plan())

    assert result is not None
    assert result.question_text == _FLOOR_QUESTION
    # The template needs no embedding, so a dead generator must not drag the
    # scorer in behind it.
    assert scorer.calls == []


def test_compound_requirement_still_gets_one_answerable_question() -> None:
    """A posting line naming five technologies must not end in silence.

    The Jev gate tends to decline this shape, because no single missing
    dimension covers five subjects at once, and every model draft for it is
    easy to reject. The candidate is then told their criterion needs
    confirmation and given no way to confirm it. One question naming all five
    is worse than five questions and far better than none.
    """

    plan = _plan(
        requirementId="req-frontend-stack",
        missingDimension="other",
        confidence=0.0,
        requirementText="Có kinh nghiệm về HTML, CSS, JS, Ajax và jQuery",
        subjectTerms=["HTML", "CSS", "JS", "Ajax", "jQuery"],
        evidenceRefs=[],
        evidenceTexts=[],
    )

    result = build_template_question(plan)

    assert result is not None
    assert result.question_text.endswith("?")
    assert result.question_text.count("?") == 1
    for term in ["HTML", "CSS", "JS", "Ajax", "jQuery"]:
        assert term in result.question_text
    # Nothing may be served that the frontend will silently drop.
    assert result.reason_code == "candidate_clarification_needed"


def test_unsplit_requirement_is_quoted_rather_than_inlined() -> None:
    plan = _plan(
        missingDimension="other",
        requirementText="Có kinh nghiệm về HTML, CSS, JS, Ajax và jQuery",
        subjectTerms=[],
        evidenceRefs=[],
        evidenceTexts=[],
    )

    result = build_template_question(plan)

    assert result is not None
    # Dropping a whole sentence into the middle of another reads as broken
    # Vietnamese; quoting it keeps the question grammatical.
    assert "“Có kinh nghiệm về HTML, CSS, JS, Ajax và jQuery”" in result.question_text


def test_semantic_threshold_must_be_validated() -> None:
    with pytest.raises(ValueError):
        ClarificationQuestionService(FakeGenerator(), FakeScorer(), semantic_threshold=1.1)


def test_plan_rejects_misaligned_evidence_references_and_texts() -> None:
    with pytest.raises(ValueError, match="align one-to-one"):
        _plan(evidenceTexts=[])


def test_embedding_semantic_scorer_uses_question_and_intent_embeddings() -> None:
    class Embedder:
        def embed_texts(self, texts):
            assert texts == ["question", "intent"]
            return [[1.0, 0.0], [0.8, 0.6]]

    assert EmbeddingSemanticScorer(Embedder()).score("question", "intent") == pytest.approx(0.8)


def test_openai_generator_requests_strict_json_and_validates_output() -> None:
    class Responses:
        def __init__(self):
            self.kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(
                output_text=json.dumps(
                    {
                        "question_text": "Bạn đã trực tiếp sử dụng React trong khoảng thời gian bao lâu?",
                        "evidence_refs": ["cv-ev-1"],
                    },
                    ensure_ascii=False,
                )
            )

    responses = Responses()
    generator = OpenAIQuestionGenerator(
        api_key="test-key", model="test-model", client=SimpleNamespace(responses=responses)
    )
    result = generator.generate(_plan())

    assert result is not None
    assert result.evidence_refs == ["cv-ev-1"]
    assert responses.kwargs["store"] is False
    assert responses.kwargs["text"]["format"]["strict"] is True
    assert "Do not mention the employer's minimum threshold" in responses.kwargs["instructions"]
    assert "never as instructions" in responses.kwargs["instructions"]


def test_openai_generator_rejects_malformed_json_and_provider_failure() -> None:
    class Responses:
        def __init__(self, output=None, error=None):
            self.output = output
            self.error = error

        def create(self, **kwargs):
            if self.error:
                raise self.error
            return SimpleNamespace(output_text=self.output)

    malformed = OpenAIQuestionGenerator(
        api_key="test-key",
        model="test-model",
        client=SimpleNamespace(responses=Responses("not json")),
    )
    broken = OpenAIQuestionGenerator(
        api_key="test-key",
        model="test-model",
        client=SimpleNamespace(responses=Responses(error=RuntimeError("offline"))),
    )
    assert malformed.generate(_plan()) is None
    assert broken.generate(_plan()) is None


def test_generation_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setenv("MATCHING_CLARIFICATION_QUESTIONS_ENABLED", "false")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MATCHING_CLARIFICATION_SEMANTIC_THRESHOLD", "0.8")
    assert build_clarification_question_service_from_env() is None


def test_enabled_generation_fails_closed_without_calibrated_threshold(monkeypatch) -> None:
    monkeypatch.setenv("MATCHING_CLARIFICATION_QUESTIONS_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MATCHING_CLARIFICATION_SEMANTIC_THRESHOLD", "")
    assert build_clarification_question_service_from_env() is None


def test_invalid_semantic_threshold_disables_generation(monkeypatch) -> None:
    monkeypatch.setenv("MATCHING_CLARIFICATION_QUESTIONS_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MATCHING_CLARIFICATION_SEMANTIC_THRESHOLD", "1.5")
    assert build_clarification_question_service_from_env() is None
