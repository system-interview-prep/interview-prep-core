"""T1.0.1–T1.0.3 — ``generate_tool_calls`` goes through the same door as ``generate_text``."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.core import pipeline_events
from src.modules.ai import facade

TOOLS = [{
    "type": "function", "name": "move_to_next_question", "strict": True,
    "description": "Move on.", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}},
                                              "required": ["reason"], "additionalProperties": False},
}]


class _FakeResponses:
    def __init__(self, outputs, fail_first_with: str | None = None):
        self.outputs = outputs
        self.fail_first_with = fail_first_with
        self.calls: list[dict] = []

    def create(self, **options):
        self.calls.append(options)
        if self.fail_first_with and len(self.calls) == 1:
            raise RuntimeError(self.fail_first_with)
        return SimpleNamespace(output=self.outputs, output_text="", usage=SimpleNamespace(input_tokens=12, output_tokens=3))


def _client_with(responses: _FakeResponses):
    return lambda: SimpleNamespace(responses=responses)


@pytest.fixture
def recorded(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(pipeline_events, "record_ai_call", lambda **row: rows.append(row))
    return rows


@pytest.mark.asyncio
async def test_tool_calls_are_parsed_and_logged(monkeypatch, recorded) -> None:
    """T1.0.1: function_call items become dicts; one ai_call_logs row with the purpose."""
    item = SimpleNamespace(type="function_call", name="move_to_next_question",
                           arguments=json.dumps({"reason": "sufficient"}), call_id="call_1")
    fake = _FakeResponses([SimpleNamespace(type="message"), item])
    monkeypatch.setattr(facade, "_client", _client_with(fake))

    calls = await facade.generate_tool_calls(
        instructions="decide", input_items="answer", tools=TOOLS, purpose="interviews.agent.decide",
    )

    assert calls == [{"name": "move_to_next_question", "arguments": {"reason": "sufficient"}, "call_id": "call_1"}]
    assert fake.calls[0]["tools"] is TOOLS and fake.calls[0]["tool_choice"] == "required"
    assert fake.calls[0]["store"] is False
    assert len(recorded) == 1
    assert recorded[0]["purpose"] == "interviews.agent.decide"
    assert recorded[0]["status"] == "ok" and recorded[0]["input_tokens"] == 12
    assert "move_to_next_question" in recorded[0]["output_text"]


@pytest.mark.asyncio
async def test_a_refused_reasoning_option_is_dropped_and_remembered(monkeypatch, recorded) -> None:
    """T1.0.2: first call refused for `reasoning`, second succeeds without it; later calls skip it."""
    facade._NO_REASONING_MODELS.discard(facade.get_settings().llm_model)
    item = SimpleNamespace(type="function_call", name="move_to_next_question", arguments="{}", call_id="c")
    fake = _FakeResponses([item], fail_first_with="reasoning is not supported for this model")
    monkeypatch.setattr(facade, "_client", _client_with(fake))

    calls = await facade.generate_tool_calls(
        instructions="d", input_items="a", tools=TOOLS, reasoning_effort="none", purpose="p",
    )

    assert [c["name"] for c in calls] == ["move_to_next_question"]
    assert "reasoning" in fake.calls[0] and "reasoning" not in fake.calls[1]
    assert facade.get_settings().llm_model in facade._NO_REASONING_MODELS
    await facade.generate_tool_calls(instructions="d", input_items="a", tools=TOOLS, reasoning_effort="none", purpose="p")
    assert "reasoning" not in fake.calls[2]
    facade._NO_REASONING_MODELS.discard(facade.get_settings().llm_model)


@pytest.mark.asyncio
async def test_text_only_answer_gives_no_calls_and_bad_json_is_kept_raw(monkeypatch, recorded) -> None:
    """T1.0.3: no function_call → []; unparsable arguments are returned under `_raw`, never raised."""
    fake = _FakeResponses([SimpleNamespace(type="message")])
    monkeypatch.setattr(facade, "_client", _client_with(fake))
    assert await facade.generate_tool_calls(instructions="d", input_items=[{"role": "user", "content": "x"}], tools=TOOLS, purpose="p") == []

    broken = SimpleNamespace(type="function_call", name="move_to_next_question", arguments="{not json", call_id="c2")
    monkeypatch.setattr(facade, "_client", _client_with(_FakeResponses([broken])))
    calls = await facade.generate_tool_calls(instructions="d", input_items="x", tools=TOOLS, purpose="p")
    assert calls[0]["arguments"] == {"_raw": "{not json"}
    assert len(recorded) == 2 and all(row["status"] == "ok" for row in recorded)


@pytest.mark.asyncio
async def test_provider_errors_are_logged_as_errors_and_re_raised(monkeypatch, recorded) -> None:
    class _Boom:
        def create(self, **_):
            raise ConnectionError("provider down")

    monkeypatch.setattr(facade, "_client", lambda: SimpleNamespace(responses=_Boom()))
    with pytest.raises(ConnectionError):
        await facade.generate_tool_calls(instructions="d", input_items="x", tools=TOOLS, purpose="p")
    assert recorded[0]["status"] == "error" and recorded[0]["error_type"] == "ConnectionError"


@pytest.mark.asyncio
async def test_a_per_call_model_overrides_the_configured_one(monkeypatch, recorded) -> None:
    """INTERVIA_INTERVIEW_AGENT_V2_SPEC §1.1: the Talker reaches its model
    through the existing provider, not a new model server.

    The override applies to one call, is what the provider is asked for, and is
    the model written to ``ai_call_logs`` so cost stays attributable per model.
    """
    item = SimpleNamespace(type="function_call", name="move_to_next_question",
                           arguments=json.dumps({"reason": "sufficient"}), call_id="call_1")
    fake = _FakeResponses([item])
    monkeypatch.setattr(facade, "_client", _client_with(fake))

    await facade.generate_tool_calls(
        instructions="predict", input_items="answer", tools=TOOLS,
        purpose="interviews.agent.talker", model="gpt-5.6-luna",
    )

    assert fake.calls[0]["model"] == "gpt-5.6-luna"
    assert recorded[0]["model"] == "gpt-5.6-luna"


@pytest.mark.asyncio
async def test_without_an_override_the_configured_model_is_used(monkeypatch, recorded) -> None:
    """The default path is unchanged: no caller has to pass a model."""
    from src.core.config import get_settings

    item = SimpleNamespace(type="function_call", name="move_to_next_question",
                           arguments=json.dumps({"reason": "sufficient"}), call_id="call_1")
    fake = _FakeResponses([item])
    monkeypatch.setattr(facade, "_client", _client_with(fake))

    await facade.generate_tool_calls(instructions="decide", input_items="answer", tools=TOOLS)

    assert fake.calls[0]["model"] == get_settings().llm_model
