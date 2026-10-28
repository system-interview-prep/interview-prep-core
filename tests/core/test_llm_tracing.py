"""Langfuse tracing of interview turns (src/core/llm_tracing.py), against an in-memory exporter."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest

from src.core import llm_tracing

langfuse = pytest.importorskip("langfuse")


@pytest.fixture
def exported(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    # The SDK keeps one set of resources per public key: a fresh key per test.
    client = langfuse.Langfuse(
        public_key=f"pk-lf-{uuid.uuid4().hex}",
        secret_key="sk-lf-test",
        base_url="http://127.0.0.1:9",
        span_exporter=exporter,
        mask=llm_tracing.mask,
    )
    monkeypatch.setattr(client, "create_score", lambda **kwargs: scores.append(kwargs))
    scores: list[dict] = []
    llm_tracing.use_client(client)
    exporter.scores = scores  # type: ignore[attr-defined]
    try:
        yield exporter
    finally:
        client.flush()
        llm_tracing.use_client(None)


def _spans(exporter) -> dict[str, dict]:
    llm_tracing.flush()
    spans = {}
    for span in exporter.get_finished_spans():
        attributes = dict(span.attributes)
        for key in ("langfuse.observation.input", "langfuse.observation.output"):
            try:
                attributes[key] = json.dumps(json.loads(attributes[key]), ensure_ascii=False)
            except (KeyError, ValueError):
                pass
        spans[span.name] = attributes
    return spans


def test_a_turn_is_one_trace_with_its_model_calls_masked(exported, monkeypatch) -> None:
    monkeypatch.setattr(llm_tracing, "text_sampled", lambda session_id: True)
    llm_tracing.register_names("Nguyễn Thị Hiền", "Hiền")
    with llm_tracing.trace(
        llm_tracing.TURN_TRACE,
        session_id="sess-1",
        user_id="user-1",
        input={"question": "Bạn xử lý cache thế nào?"},
        private_input={"answer": "Em là Hiền, mail a@b.com"},
        metadata={"turn_index": "2"},
    ) as turn:
        generation = llm_tracing.Generation(
            "interviews.interview_engine.InterviewCoreEngine._generate_probe_question",
            model="gpt-test",
            instructions="SYSTEM",
            input_text="Hiền gọi 0912345678",
        )
        generation.end(output="Khi Redis lỗi thì sao?")
        llm_tracing.finish(turn, output={"action": "PROBE"}, flags=[])
    spans = _spans(exported)
    root, probe = spans["interview_turn"], spans["probe"]
    assert root["session.id"] == "sess-1" and root["user.id"] != "user-1"  # hashed
    assert "[tên]" in root["langfuse.observation.input"] and "[email]" in root["langfuse.observation.input"]
    assert probe["langfuse.observation.type"] == "generation"
    assert "[sđt]" in probe["langfuse.observation.input"]
    assert probe["langfuse.version"] == llm_tracing.prompt_version("SYSTEM")
    assert {s["name"]: s["value"] for s in exported.scores} == {"rule.flagged": 0}


def test_unsampled_turn_sends_no_text_until_it_is_flagged(exported, monkeypatch) -> None:
    monkeypatch.setattr(llm_tracing, "text_sampled", lambda session_id: False)
    with llm_tracing.trace(
        llm_tracing.TURN_TRACE,
        session_id="sess-2",
        input={"question": "Q?"},
        private_input={"answer": "secret"},
    ) as turn:
        generation = llm_tracing.Generation(
            "x._evaluate_candidate_response", model="m", instructions="S", input_text="secret"
        )
        generation.end(output='{"intent": "ANSWER"}')
        llm_tracing.finish(
            turn,
            output={"action": "PROBE"},
            flags=["probe_off_target"],
            private_input={"question": "Q?", "answer": "secret"},
            scores={"guard.rejects": 1},
        )
    spans = _spans(exported)
    assert "langfuse.observation.input" not in spans["evaluate_answer"]
    root = spans["interview_turn"]
    assert "secret" in root["langfuse.observation.input"]  # flagged: the turn carries its text
    assert "flag:probe_off_target" in root["langfuse.trace.tags"] and "flagged" in root["langfuse.trace.tags"]
    assert "model_outputs" in root["langfuse.observation.output"]
    names = {s["name"]: s["value"] for s in exported.scores}
    assert names == {"rule.flagged": 1, "flag.probe_off_target": 1, "guard.rejects": 1.0}


def test_everything_is_a_no_op_without_langfuse() -> None:
    llm_tracing.use_client(None)
    with llm_tracing.trace(llm_tracing.TURN_TRACE, session_id="s") as turn:
        assert turn is None
        llm_tracing.Generation("x.y", model="m", instructions="i", input_text="t").end(output="o")
        llm_tracing.finish(turn, output={}, flags=["x"])
    llm_tracing.score_trace("abc", "human.guard_correct", 1)


def test_step_names_and_masking_helpers() -> None:
    assert (
        llm_tracing.step_of("interviews.interview_engine.InterviewCoreEngine._generate_clarify_text")
        == "clarify"
    )
    assert llm_tracing.step_of("matching.llm.score") == "score"
    assert llm_tracing.mask(data={"a": ["gọi 0987 654 321", "x@y.vn"]}) == {"a": ["gọi [sđt]", "[email]"]}
