"""The gate judge may demote a posting line; it may never promote one.

Three layers are exercised with no model involved: the judge's own
verification of what the model said, the policy ``classify_requirement_gate``
applies to a reading, and what the adapter does with the result.
"""

from __future__ import annotations

import json

import pytest

from src.core.config import Settings
from src.modules.job_descriptions.schemas import CanonicalJobDescription
from src.modules.matching.domain.adapters import job_description_to_matching_job
from src.modules.matching.domain.requirement_gating import classify_requirement_gate
from src.modules.matching.judge import gate_judge
from src.modules.matching.judge.gate_judge import GateJudge, gate_reading_for
from src.modules.matching.judge.readings import GateReading


class ScriptedClient:
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


def _answer(relation: str, cue: str = "") -> str:
    return json.dumps({"relation": relation, "cue_quote": cue}, ensure_ascii=False)


def _reading(label: str, relation: str, cue: str | None = None) -> GateReading:
    return GateReading(rawLabel=label, relation=relation, cueQuote=cue)


@pytest.fixture(autouse=True)
def _clear_memo():
    gate_judge.reset_gate_memo()
    yield
    gate_judge.reset_gate_memo()


# --- the judge verifies the model ------------------------------------------


def test_a_demotion_with_a_verbatim_cue_is_kept() -> None:
    judge = GateJudge(ScriptedClient(_answer("soft_signal", "khả năng thích nghi")))

    reading = judge.read("Có khả năng thích nghi với môi trường mới", "other")

    assert reading is not None
    assert reading.relation == "soft_signal"
    assert reading.cue_quote == "khả năng thích nghi"


def test_a_demotion_whose_cue_is_not_in_the_line_becomes_unsure() -> None:
    judge = GateJudge(ScriptedClient(_answer("soft_signal", "năng động")))

    reading = judge.read("Có khả năng thích nghi với môi trường mới", "other")

    assert reading is not None
    assert reading.relation == "unsure"
    assert reading.cue_quote is None


def test_a_demotion_with_no_cue_at_all_becomes_unsure() -> None:
    judge = GateJudge(ScriptedClient(_answer("non_assessable")))

    reading = judge.read("Ngoại hình ưa nhìn", "other")

    assert reading is not None and reading.relation == "unsure"


def test_objective_needs_no_cue() -> None:
    judge = GateJudge(ScriptedClient(_answer("objective")))

    reading = judge.read("Thành thạo Java", "skill")

    assert reading is not None and reading.relation == "objective"


def test_broken_json_is_retried_once_then_none() -> None:
    client = ScriptedClient("{", "nope")
    judge = GateJudge(client)

    assert judge.read("x", None) is None
    assert client.calls == 2


def test_a_relation_outside_the_enum_is_none() -> None:
    judge = GateJudge(ScriptedClient(_answer("vacuous"), _answer("vacuous")))

    assert judge.read("x", None) is None


def test_transport_failure_is_none_without_retry() -> None:
    client = ScriptedClient(ConnectionError("down"))

    assert GateJudge(client).read("x", None) is None
    assert client.calls == 1


# --- the policy in classify_requirement_gate ---------------------------------


LINE = "Có khả năng thích nghi với môi trường mới"


def test_the_rules_alone_call_the_unfamiliar_wording_objective() -> None:
    """The premise: this wording is not in the phrase table, so it would gate."""
    assert classify_requirement_gate(LINE, "other").gate_class == "objective"


def test_a_soft_signal_reading_demotes_an_objective_line() -> None:
    decision = classify_requirement_gate(LINE, "other", _reading(LINE, "soft_signal", "thích nghi"))

    assert decision.gate_class == "soft_signal"
    assert decision.reason_code == "judge_attitude_not_verifiable_from_cv"
    assert not decision.may_gate


@pytest.mark.parametrize("relation", ["objective", "unsure"])
def test_objective_and_unsure_readings_change_nothing(relation: str) -> None:
    assert classify_requirement_gate(LINE, "other", _reading(LINE, relation)) == classify_requirement_gate(
        LINE, "other"
    )


def test_a_reading_cannot_promote_a_line_the_rules_call_soft() -> None:
    label = "Năng động, sáng tạo"
    rule = classify_requirement_gate(label, "other")
    assert rule.gate_class == "soft_signal"

    assert classify_requirement_gate(label, "other", _reading(label, "objective")) == rule


@pytest.mark.parametrize(
    "label",
    [
        "Tốt nghiệp Đại học CNTT, nhiệt tình và chịu khó",
        "Thành thạo Java và Spring Boot, tư duy tốt",
        "Tối thiểu 2 năm kinh nghiệm, năng động",
        "Giao tiếp tiếng Anh tốt",
    ],
)
def test_an_anchored_line_keeps_its_gate_whatever_the_reading_says(label: str) -> None:
    decision = classify_requirement_gate(label, "other", _reading(label, "soft_signal", label[:10]))

    assert decision.gate_class == "objective"
    assert decision.may_gate


def test_non_assessable_and_optional_demotions_carry_their_own_reason() -> None:
    assert (
        classify_requirement_gate(LINE, None, _reading(LINE, "non_assessable", "thích nghi")).reason_code
        == "judge_not_evidenced_by_a_cv"
    )
    assert (
        classify_requirement_gate(LINE, None, _reading(LINE, "optional", "thích nghi")).reason_code
        == "judge_posting_marks_requirement_optional"
    )


# --- gate_reading_for: off, unavailable, memoised ----------------------------


def test_disabled_asks_nothing() -> None:
    client = ScriptedClient(_answer("soft_signal", "x"))

    assert (
        gate_reading_for("x", judge=GateJudge(client), settings=Settings(matching_judge_enabled=False))
        is None
    )
    assert client.calls == 0


def test_enabled_without_a_key_is_none() -> None:
    settings = Settings(matching_judge_enabled=True, openai_api_key=None)

    assert gate_reading_for("x", settings=settings) is None


def test_the_same_line_is_read_once_per_process() -> None:
    client = ScriptedClient(_answer("soft_signal", "thích nghi"))
    settings = Settings(matching_judge_enabled=True, openai_api_key=None)
    judge = GateJudge(client)

    first = gate_reading_for(LINE, "other", judge=judge, settings=settings)
    second = gate_reading_for(LINE, "other", judge=judge, settings=settings)

    assert first == second
    assert first is not None and first.relation == "soft_signal"
    assert client.calls == 1


# --- the adapter: what a soft signal becomes ---------------------------------


def _parsed(label: str) -> CanonicalJobDescription:
    return CanonicalJobDescription.model_validate(
        {
            "schemaVersion": "1.0",
            "jobTitle": "Intern",
            "requirements": [
                {
                    "requirementId": "req-soft",
                    "kind": "other",
                    "priority": "must_have",
                    "rawLabel": label,
                    "evidenceRefs": ["jd-ev"],
                }
            ],
            "evidence": [
                {
                    "evidenceId": "jd-ev",
                    "documentId": "jd-doc",
                    "documentSha256": "b" * 64,
                    "section": "requirements",
                    "text": label,
                    "charStart": 0,
                    "charEnd": len(label),
                }
            ],
            "parsing": {
                "parserVersion": "test",
                "extractionVersion": "test",
                "parsedAt": "2026-01-01T00:00:00Z",
                "status": "ready",
            },
        }
    )


def test_a_soft_signal_is_context_not_a_scored_requirement() -> None:
    """A CV cannot evidence "năng động, sáng tạo"; scoring it only lowered everyone."""
    job = job_description_to_matching_job(_parsed("Năng động, sáng tạo"), job_id="job-1")

    assert job.requirements[0].priority == "context"


def test_an_optional_item_still_scores_as_nice_to_have() -> None:
    job = job_description_to_matching_job(_parsed("Có kiến thức về API là một lợi thế"), job_id="job-1")

    assert job.requirements[0].priority == "nice_to_have"
