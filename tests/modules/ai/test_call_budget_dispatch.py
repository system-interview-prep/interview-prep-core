"""The provider-call budget is charged at dispatch, once per attempt.

The §13 pilot spent 4x its attributed budget on calls no counter saw. These
tests pin the one place every call now passes: ``facade._responses_create``
charges ``ai.budget`` before each request -- the first attempt, the retry
after a refused option, the retry after a transport error -- and a scope
with nothing left never reaches the provider.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import openai
import pytest

from src.core import pipeline_events
from src.modules.ai import budget, facade


class _Responses:
    def __init__(self, *, fail_first_with: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.fail_first_with = fail_first_with

    def create(self, **options):
        self.calls.append(dict(options))
        if self.fail_first_with is not None and len(self.calls) == 1:
            raise self.fail_first_with
        return SimpleNamespace(output_text="ok", output=[], usage=None)


@pytest.fixture
def provider(monkeypatch):
    responses = _Responses()
    monkeypatch.setattr(facade, "_client", lambda: SimpleNamespace(responses=responses))
    monkeypatch.setattr(facade, "TRANSPORT_RETRY_DELAY_SECONDS", 0)
    budget.reset_session_ledger()
    return responses


@pytest.fixture
def recorded(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(pipeline_events, "record_ai_call", lambda **row: rows.append(row))
    return rows


def _connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))


@pytest.mark.asyncio
async def test_without_a_scope_nothing_is_charged(provider) -> None:
    assert budget.current_call_budget.get() is None
    for _ in range(3):
        assert await facade.generate_text(instructions="s", input_text="i") == "ok"
    assert len(provider.calls) == 3


@pytest.mark.asyncio
async def test_a_scope_with_nothing_left_never_reaches_the_provider(provider, recorded) -> None:
    with budget_scope_of(limit=1) as scope:
        assert await facade.generate_text(instructions="s", input_text="i", purpose="t.first") == "ok"
        with pytest.raises(budget.CallBudgetExceeded) as refused:
            await facade.generate_text(instructions="s", input_text="i", purpose="t.second")
    assert len(provider.calls) == 1, "the second request was not sent"
    assert scope.spent == 1 and scope.charged == ["t.first"]
    assert refused.value.level == "scope" and refused.value.purpose == "t.second"
    # The refusal is a visible failed call, not a silent skip.
    assert recorded[-1]["status"] == "error" and recorded[-1]["error_type"] == "CallBudgetExceeded"
    assert recorded[-1]["purpose"] == "t.second"


@pytest.mark.asyncio
async def test_the_retry_after_a_refused_option_is_charged_too(monkeypatch, recorded) -> None:
    responses = _Responses(fail_first_with=RuntimeError("reasoning is not supported for this model"))
    monkeypatch.setattr(facade, "_client", lambda: SimpleNamespace(responses=responses))
    facade._NO_REASONING_MODELS.discard("gpt-budget-test")
    budget.reset_session_ledger()
    with budget_scope_of(limit=2) as scope:
        await facade.generate_text(instructions="s", input_text="i", reasoning_effort="low", model="gpt-budget-test")
    assert len(responses.calls) == 2 and scope.spent == 2
    facade._NO_REASONING_MODELS.discard("gpt-budget-test")
    responses.calls.clear()
    with budget_scope_of(limit=1):
        with pytest.raises(budget.CallBudgetExceeded):
            await facade.generate_text(instructions="s", input_text="i", reasoning_effort="low", model="gpt-budget-test")
    assert len(responses.calls) == 1, "the option-drop retry was refused, not sent"
    facade._NO_REASONING_MODELS.discard("gpt-budget-test")


@pytest.mark.asyncio
async def test_a_transport_error_is_retried_once_and_the_retry_is_charged(monkeypatch) -> None:
    responses = _Responses(fail_first_with=_connection_error())
    monkeypatch.setattr(facade, "_client", lambda: SimpleNamespace(responses=responses))
    monkeypatch.setattr(facade, "TRANSPORT_RETRY_DELAY_SECONDS", 0)
    budget.reset_session_ledger()
    with budget_scope_of(limit=2) as scope:
        assert await facade.generate_text(instructions="s", input_text="i") == "ok"
    assert len(responses.calls) == 2 and scope.spent == 2

    responses = _Responses(fail_first_with=_connection_error())
    monkeypatch.setattr(facade, "_client", lambda: SimpleNamespace(responses=responses))
    with budget_scope_of(limit=1):
        with pytest.raises(budget.CallBudgetExceeded):
            await facade.generate_text(instructions="s", input_text="i")
    assert len(responses.calls) == 1, "no budget for the retry: the error stands, nothing more is sent"


@pytest.mark.asyncio
async def test_a_second_transport_error_is_not_retried_again(monkeypatch) -> None:
    class _AlwaysDown(_Responses):
        def create(self, **options):
            self.calls.append(dict(options))
            raise _connection_error()

    responses = _AlwaysDown()
    monkeypatch.setattr(facade, "_client", lambda: SimpleNamespace(responses=responses))
    monkeypatch.setattr(facade, "TRANSPORT_RETRY_DELAY_SECONDS", 0)
    with budget_scope_of(limit=10):
        with pytest.raises(openai.APIConnectionError):
            await facade.generate_text(instructions="s", input_text="i")
    assert len(responses.calls) == 2


def test_only_the_errors_the_sdk_used_to_retry_are_retried() -> None:
    assert facade._retryable_transport_error(_connection_error())
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    assert facade._retryable_transport_error(
        openai.RateLimitError("slow down", response=httpx.Response(429, request=request), body=None)
    )
    assert facade._retryable_transport_error(
        openai.InternalServerError("boom", response=httpx.Response(503, request=request), body=None)
    )
    assert not facade._retryable_transport_error(
        openai.AuthenticationError("no", response=httpx.Response(401, request=request), body=None)
    )
    assert not facade._retryable_transport_error(ValueError("schema"))


@pytest.mark.asyncio
async def test_the_session_ledger_spans_scopes_in_one_process(provider) -> None:
    with budget.budget_scope("turn:s9:1", limit=5, session_id="s9", session_limit=3):
        await facade.generate_text(instructions="s", input_text="i")
        await facade.generate_text(instructions="s", input_text="i")
    with budget.budget_scope("preview:s9", limit=5, session_id="s9", session_limit=3) as scope:
        await facade.generate_text(instructions="s", input_text="i")
        with pytest.raises(budget.CallBudgetExceeded) as refused:
            await facade.generate_text(instructions="s", input_text="i")
    assert refused.value.level == "session" and refused.value.spent == 3
    assert scope.spent == 1, "the refused attempt did not count against the scope either"
    assert budget.session_spent("s9") == 3 and len(provider.calls) == 3
    # Another session is unaffected.
    with budget.budget_scope("turn:s10:1", limit=5, session_id="s10", session_limit=3):
        await facade.generate_text(instructions="s", input_text="i")
    assert budget.session_spent("s10") == 1


def test_a_zero_limit_scope_refuses_the_first_call() -> None:
    """A deterministic prefetch runs under limit 0: a provider call there is a bug, and it is caught."""
    with budget.budget_scope("prefetch:s1", limit=0):
        with pytest.raises(budget.CallBudgetExceeded):
            budget.charge("anything")


def test_the_sdk_client_keeps_no_retries_of_its_own(monkeypatch) -> None:
    built: dict = {}

    class _OpenAI:
        def __init__(self, **kwargs):
            built.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", _OpenAI)
    facade._build_client("sk-test", 12.0)
    assert built == {"api_key": "sk-test", "timeout": 12.0, "max_retries": 0}
    assert facade.SDK_MAX_RETRIES == 0


def budget_scope_of(*, limit: int):
    return budget.budget_scope("test", limit=limit)
