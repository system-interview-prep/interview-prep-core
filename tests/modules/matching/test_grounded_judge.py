"""The judge's answer is verified, never trusted.

Every case here feeds the judge a payload a real model could plausibly emit
and checks that what reaches the evaluators is only what survived the
grounding checks: ids it was handed, quotes found verbatim in those spans,
and a ``satisfies`` with at least one such span behind it.
"""

from __future__ import annotations

import json

import pytest

from src.modules.matching.judge.grounded_judge import GroundedJudge, quote_is_grounded
from src.modules.user_cvs.schemas import EvidenceSpan

SHA = "a" * 64


def _span(evidence_id: str, text: str, section: str = "employment") -> EvidenceSpan:
    return EvidenceSpan.model_validate(
        {
            "evidenceId": evidence_id,
            "documentId": "cv-1",
            "documentSha256": SHA,
            "section": section,
            "text": text,
            "charStart": 0,
            "charEnd": len(text),
        }
    )


CANDIDATES = [
    _span("cv-1", "Built REST APIs with Java, Spring Boot and PostgreSQL."),
    _span("cv-2", "Tối ưu truy vấn SQL, giảm thời gian phản hồi từ 800ms xuống 120ms."),
]


class ScriptedClient:
    """Returns the scripted outputs in order; raises once the script runs out."""

    def __init__(self, *outputs: str | Exception) -> None:
        self._outputs = list(outputs)
        self.calls = 0

    def complete(self, *, instructions: str, input_text: str, output_schema: dict) -> str:
        self.calls += 1
        if not self._outputs:
            raise RuntimeError("no scripted output left")
        item = self._outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _judge(*outputs: str | Exception) -> tuple[GroundedJudge, ScriptedClient]:
    client = ScriptedClient(*outputs)
    return GroundedJudge(client), client


def _verdict(relation: str, *selected: tuple[str, str]) -> str:
    return json.dumps(
        {"relation": relation, "selected": [{"evidence_id": i, "quote": q} for i, q in selected]},
        ensure_ascii=False,
    )


def test_a_grounded_satisfies_is_kept_with_its_spans() -> None:
    judge, _ = _judge(_verdict("satisfies", ("cv-2", "giảm thời gian phản hồi từ 800ms xuống 120ms")))

    outcome = judge.judge(requirement_id="req-1", requirement_text="Tối ưu hiệu năng", candidates=CANDIDATES)

    assert outcome.fallback is None
    assert outcome.reading is not None
    assert outcome.reading.relation == "satisfies"
    assert [item.evidence_id for item in outcome.reading.selected] == ["cv-2"]


def test_an_id_the_judge_was_not_given_is_dropped() -> None:
    judge, _ = _judge(_verdict("satisfies", ("cv-99", "anything"), ("cv-1", "Spring Boot")))

    outcome = judge.judge(requirement_id="req-1", requirement_text="Spring", candidates=CANDIDATES)

    assert outcome.reading is not None
    assert [item.evidence_id for item in outcome.reading.selected] == ["cv-1"]
    assert outcome.reading.dropped == {"cv-99": "unknown_id"}


def test_a_quote_not_in_the_span_is_dropped() -> None:
    judge, _ = _judge(_verdict("satisfies", ("cv-1", "Built GraphQL APIs with Kotlin")))

    outcome = judge.judge(requirement_id="req-1", requirement_text="Kotlin", candidates=CANDIDATES)

    assert outcome.reading is not None
    # The model claimed satisfaction and could not point at a single real line.
    assert outcome.reading.relation == "unsure"
    assert outcome.reading.selected == []
    assert outcome.reading.dropped == {"cv-1": "quote_not_in_span"}
    assert outcome.fallback == "no_valid_span"


@pytest.mark.parametrize("relation", ["partially", "unrelated", "unsure"])
def test_non_satisfying_relations_pass_through_unchanged(relation: str) -> None:
    judge, _ = _judge(_verdict(relation))

    outcome = judge.judge(requirement_id="req-1", requirement_text="x", candidates=CANDIDATES)

    assert outcome.reading is not None
    assert outcome.reading.relation == relation
    assert outcome.fallback is None


def test_broken_json_is_retried_once_then_falls_back() -> None:
    judge, client = _judge('{"relation": "satisf', "not json either")

    outcome = judge.judge(requirement_id="req-1", requirement_text="x", candidates=CANDIDATES)

    assert outcome.reading is None
    assert outcome.fallback == "schema"
    assert client.calls == 2


def test_a_relation_outside_the_enum_is_a_schema_failure() -> None:
    judge, _ = _judge(_verdict("rejected"), _verdict("rejected"))

    outcome = judge.judge(requirement_id="req-1", requirement_text="x", candidates=CANDIDATES)

    assert outcome.reading is None
    assert outcome.fallback == "schema"


def test_extra_fields_are_refused() -> None:
    payload = json.dumps({"relation": "satisfies", "selected": [], "verdict": "not_met"})
    judge, _ = _judge(payload, payload)

    outcome = judge.judge(requirement_id="req-1", requirement_text="x", candidates=CANDIDATES)

    assert outcome.fallback == "schema"


def test_a_transport_error_falls_back_without_retry() -> None:
    judge, client = _judge(ConnectionError("down"))

    outcome = judge.judge(requirement_id="req-1", requirement_text="x", candidates=CANDIDATES)

    assert outcome.reading is None
    assert outcome.fallback == "transport"
    assert client.calls == 1


def test_no_candidates_means_no_call() -> None:
    judge, client = _judge(_verdict("satisfies", ("cv-1", "Spring Boot")))

    outcome = judge.judge(requirement_id="req-1", requirement_text="x", candidates=[])

    assert outcome.fallback == "no_candidates"
    assert client.calls == 0


def test_broken_json_after_a_good_retry_recovers() -> None:
    judge, client = _judge("{", _verdict("satisfies", ("cv-1", "Spring Boot")))

    outcome = judge.judge(requirement_id="req-1", requirement_text="x", candidates=CANDIDATES)

    assert outcome.reading is not None and outcome.reading.relation == "satisfies"
    assert client.calls == 2


@pytest.mark.parametrize(
    ("quote", "span", "grounded"),
    [
        ("Spring Boot", "Built REST APIs with Java, Spring Boot and PostgreSQL.", True),
        ("spring   boot", "Built REST APIs with Java, Spring\nBoot and PostgreSQL.", True),
        ("Spring Boots", "Built REST APIs with Java, Spring Boot and PostgreSQL.", False),
        ("Java and PostgreSQL", "Built REST APIs with Java, Spring Boot and PostgreSQL.", False),
        ("ab", "ab cd", False),
    ],
)
def test_quote_grounding_is_verbatim_up_to_whitespace_and_case(quote: str, span: str, grounded: bool) -> None:
    assert quote_is_grounded(quote, span) is grounded
