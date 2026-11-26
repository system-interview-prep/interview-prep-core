"""``generate_text_stream``: token deltas through the same door as every other call.

Streamed output, not streamed input: the Responses API keeps no state between
requests here. One ``ai_call_logs`` row at the end with the usage the provider
reports, the budget charged before the request, the dropped-option memory.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core import pipeline_events
from src.modules.ai import budget, facade


def _delta(text: str):
    return SimpleNamespace(type="response.output_text.delta", delta=text)


def _completed(input_tokens=30, output_tokens=9):
    return SimpleNamespace(type="response.completed",
                           response=SimpleNamespace(usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)))


class _Responses:
    def __init__(self, events, *, fail_first_with: Exception | None = None, fail_after: int | None = None) -> None:
        self.events = events
        self.fail_first_with = fail_first_with
        self.fail_after = fail_after
        self.calls: list[dict] = []

    async def create(self, **options):
        self.calls.append(dict(options))
        if self.fail_first_with is not None and len(self.calls) == 1:
            raise self.fail_first_with
        return self._iter()

    async def _iter(self):
        for index, event in enumerate(self.events):
            if self.fail_after is not None and index == self.fail_after:
                raise RuntimeError("stream broke")
            yield event


@pytest.fixture
def recorded(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(pipeline_events, "record_ai_call", lambda **row: rows.append(row))
    budget.reset_session_ledger()
    return rows


@pytest.mark.asyncio
async def test_deltas_are_yielded_in_order_and_one_row_is_recorded_with_usage(monkeypatch, recorded) -> None:
    responses = _Responses([_delta("kind: reflective\n"), _delta("Ừ, mình "), _delta("hình dung được."), _completed()])
    monkeypatch.setattr(facade, "_async_client", lambda: SimpleNamespace(responses=responses))

    pieces = [p async for p in facade.generate_text_stream(instructions="s", input_text="i", purpose="t.stream", model="gpt-5.6-luna")]

    assert pieces == ["kind: reflective\n", "Ừ, mình ", "hình dung được."]
    assert responses.calls[0]["stream"] is True and responses.calls[0]["store"] is False
    assert responses.calls[0]["model"] == "gpt-5.6-luna"
    assert len(recorded) == 1
    row = recorded[0]
    assert row["purpose"] == "t.stream" and row["status"] == "ok"
    assert row["input_tokens"] == 30 and row["output_tokens"] == 9
    assert row["output_text"] == "kind: reflective\nỪ, mình hình dung được."


@pytest.mark.asyncio
async def test_the_stream_is_charged_to_the_budget_before_it_is_sent(monkeypatch, recorded) -> None:
    responses = _Responses([_delta("x"), _completed()])
    monkeypatch.setattr(facade, "_async_client", lambda: SimpleNamespace(responses=responses))
    with budget.budget_scope("bc", limit=1) as scope:
        assert [p async for p in facade.generate_text_stream(instructions="s", input_text="i")] == ["x"]
        with pytest.raises(budget.CallBudgetExceeded):
            async for _ in facade.generate_text_stream(instructions="s", input_text="i"):
                pass
    assert scope.spent == 1 and len(responses.calls) == 1
    assert recorded[-1]["status"] == "error" and recorded[-1]["error_type"] == "CallBudgetExceeded"


@pytest.mark.asyncio
async def test_a_refused_option_is_dropped_and_retried_before_any_token(monkeypatch, recorded) -> None:
    responses = _Responses([_delta("ok"), _completed()], fail_first_with=RuntimeError("reasoning is not supported"))
    monkeypatch.setattr(facade, "_async_client", lambda: SimpleNamespace(responses=responses))
    facade._NO_REASONING_MODELS.discard("gpt-stream-test")
    try:
        pieces = [p async for p in facade.generate_text_stream(instructions="s", input_text="i", reasoning_effort="none", model="gpt-stream-test")]
    finally:
        facade._NO_REASONING_MODELS.discard("gpt-stream-test")
    assert pieces == ["ok"]
    assert "reasoning" in responses.calls[0] and "reasoning" not in responses.calls[1]
    assert len(recorded) == 1 and recorded[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_an_error_after_tokens_flowed_ends_the_stream_and_is_recorded(monkeypatch, recorded) -> None:
    responses = _Responses([_delta("a"), _delta("b"), _completed()], fail_after=1)
    monkeypatch.setattr(facade, "_async_client", lambda: SimpleNamespace(responses=responses))
    got: list[str] = []
    with pytest.raises(RuntimeError):
        async for p in facade.generate_text_stream(instructions="s", input_text="i"):
            got.append(p)
    assert got == ["a"] and len(responses.calls) == 1, "no silent retry once output has started"
    assert recorded[-1]["status"] == "error" and recorded[-1]["output_text"] == "a"


def test_the_async_client_is_built_without_sdk_retries(monkeypatch) -> None:
    import openai

    built: dict = {}

    class _Async:
        def __init__(self, **kwargs):
            built.update(kwargs)

    monkeypatch.setattr(openai, "AsyncOpenAI", _Async)
    facade._build_async_client("sk-test", 9.0)
    assert built == {"api_key": "sk-test", "timeout": 9.0, "max_retries": 0}
