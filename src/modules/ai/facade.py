"""Public OpenAI adapter shared by business modules."""

import time
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any, TypedDict

from fastapi.concurrency import run_in_threadpool

from src.infrastructure import langfuse
from src.core.config import get_settings
from src.core import pipeline_events
from src.core.pipeline_events import ai_call_timer, caller_purpose
from src.modules.ai import budget
from src.modules.ai.cache_store import (  # noqa: F401 - re-exported for other modules
    CacheStore,
    FileCacheStore,
    SqlCacheStore,
    cache_store_for,
)
from src.modules.ai.structured import (  # noqa: F401 - re-exported for other modules
    CachingStructuredClient,
    OpenAIStructuredClient,
    StructuredClient,
)

# Models that answered "temperature is not supported" once. Every later call
# skips the parameter instead of paying for a rejected request first.
_NO_TEMPERATURE_MODELS: set[str] = set()
# Same for a reasoning setting or a JSON schema a model or endpoint refused.
_NO_REASONING_MODELS: set[str] = set()
_NO_SCHEMA_MODELS: set[str] = set()
# Reasoning models (GPT-5.x, o-series) refuse temperature: skip it from the first
# call. Before 25/10 every process paid one rejected request per model.
_REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _takes_temperature(model: str) -> bool:
    return model not in _NO_TEMPERATURE_MODELS and not model.casefold().startswith(_REASONING_MODEL_PREFIXES)


#: The SDK's own retries are off: the one transport retry lives in
#: ``_responses_create`` where the call budget can see it. An SDK-internal
#: retry would be a request nobody counted.
SDK_MAX_RETRIES = 0


def _build_client(api_key: str, timeout: float):
    from openai import OpenAI

    # The SDK default is a 600s read timeout with 2 retries, so a stalled
    # provider could hold an interview turn for half an hour. Callers already
    # fall back on provider errors; fail fast enough for them to do so.
    return OpenAI(api_key=api_key, timeout=timeout, max_retries=SDK_MAX_RETRIES)


@lru_cache
def _client():
    settings = get_settings()
    api_key = settings.openai_api_key
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    return _build_client(api_key, settings.ai_request_timeout_seconds)


def _build_async_client(api_key: str, timeout: float):
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=api_key, timeout=timeout, max_retries=SDK_MAX_RETRIES)


@lru_cache
def _async_client():
    """The async SDK client, for streaming only (the sync one runs in a threadpool)."""
    settings = get_settings()
    api_key = settings.openai_api_key
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    return _build_async_client(api_key, settings.ai_request_timeout_seconds)


def _retryable_transport_error(exc: BaseException) -> bool:
    """The errors the SDK used to retry on its own: connection, timeout, 408/409/429/5xx."""
    try:
        import openai
    except ImportError:  # pragma: no cover - the SDK is installed wherever this runs
        return False
    if isinstance(exc, openai.APIConnectionError):  # APITimeoutError is a subclass
        return True
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", None)
        return status in (408, 409, 429) or (isinstance(status, int) and status >= 500)
    return False


#: Pause before the one transport retry; the SDK's first backoff was 0.5 s too.
TRANSPORT_RETRY_DELAY_SECONDS = 0.5


def _responses_create(options: dict, model: str, *, purpose: str = "") -> Any:
    """``responses.create`` that drops an option the model or endpoint refuses.

    A model that answered "temperature is not supported", or an endpoint that
    refused ``reasoning`` or a JSON schema, is remembered so later calls skip the
    option instead of paying for a rejected request first.

    **Every attempt is charged to the call budget first** (``ai.budget``): the
    retry after a refused option, and the single retry after a transport error
    (connection, timeout, 408/409/429/5xx -- what the SDK retried silently
    before ``max_retries=0``). A scope with nothing left raises
    :class:`budget.CallBudgetExceeded` *before* the request is sent; the
    surrounding ``ai_call_timer`` records that as the call's error, so the
    refusal is visible in ``ai_call_logs`` like any other failed call.
    """
    transport_retried = False
    for _ in range(5):
        budget.charge(purpose or caller_purpose(3))
        try:
            return _client().responses.create(**options)
        except Exception as exc:
            message = str(exc).lower()
            if "temperature" in message and "temperature" in options:
                _NO_TEMPERATURE_MODELS.add(model)
                options.pop("temperature", None)
            elif "reasoning" in message and "reasoning" in options:
                _NO_REASONING_MODELS.add(model)
                options.pop("reasoning", None)
            elif ("schema" in message or "format" in message) and "text" in options:
                _NO_SCHEMA_MODELS.add(model)
                options.pop("text", None)
            elif not transport_retried and _retryable_transport_error(exc):
                transport_retried = True
                time.sleep(TRANSPORT_RETRY_DELAY_SECONDS)
            else:
                raise
    raise RuntimeError("model call failed after dropping unsupported options")


def _drop_refused_option(exc: Exception, options: dict, model: str) -> bool:
    """Forget the option the provider refused, so the next attempt omits it. False when it was not that."""
    message = str(exc).lower()
    if "temperature" in message and "temperature" in options:
        _NO_TEMPERATURE_MODELS.add(model)
        options.pop("temperature", None)
        return True
    if "reasoning" in message and "reasoning" in options:
        _NO_REASONING_MODELS.add(model)
        options.pop("reasoning", None)
        return True
    if ("schema" in message or "format" in message) and "text" in options:
        _NO_SCHEMA_MODELS.add(model)
        options.pop("text", None)
        return True
    return False


async def _responses_stream(options: dict, model: str, *, purpose: str) -> AsyncIterator[Any]:
    """``responses.create(stream=True)`` with the same budget and option-drop rules as the sync path.

    Every attempt is charged before it is sent. A refused option or a
    transport error is retried only while nothing has been yielded yet: once
    tokens have flowed, an error ends the stream and the caller sees it. The
    Responses API keeps no state between requests here (``store`` is false,
    no ``previous_response_id``): there is no persistent incremental input,
    only incremental *output*, which is what this streams.
    """
    transport_retried = False
    for _ in range(5):
        budget.charge(purpose)
        yielded = False
        try:
            stream = await _async_client().responses.create(**options, stream=True)
            async for event in stream:
                yielded = True
                yield event
            return
        except Exception as exc:
            if yielded:
                raise
            if _drop_refused_option(exc, options, model):
                continue
            if not transport_retried and _retryable_transport_error(exc):
                transport_retried = True
                time.sleep(0)  # no blocking sleep on the event loop; the retry is immediate
                continue
            raise
    raise RuntimeError("model call failed after dropping unsupported options")


async def generate_text_stream(
    *,
    instructions: str,
    input_text: str,
    max_output_tokens: int = 160,
    temperature: float | None = None,
    purpose: str | None = None,
    reasoning_effort: str | None = None,
    model: str | None = None,
) -> AsyncIterator[str]:
    """One model call whose text arrives as it is generated (token deltas).

    Same door as :func:`generate_text`: one ``ai_call_logs`` row (written when
    the stream ends, with the usage the provider reports in
    ``response.completed``), one Langfuse generation, the call budget charged
    before the request, the dropped-option memory. The caller decides what a
    *unit* is -- this yields raw deltas and never speaks them.
    """
    settings = get_settings()
    purpose = purpose or caller_purpose()
    model = model or settings.llm_model
    options: dict = {
        "model": model,
        "instructions": instructions,
        "input": input_text,
        "max_output_tokens": max_output_tokens,
        "store": False,
    }
    if temperature is not None and _takes_temperature(model):
        options["temperature"] = temperature
    if reasoning_effort and model not in _NO_REASONING_MODELS:
        options["reasoning"] = {"effort": reasoning_effort}
    generation = langfuse.Generation(
        purpose,
        model=model,
        instructions=instructions,
        input_text=input_text,
        options={"max_output_tokens": max_output_tokens, "reasoning_effort": reasoning_effort or "", "stream": "1"},
    )
    started = time.perf_counter()
    pieces: list[str] = []
    usage: Any = None
    error: BaseException | None = None
    try:
        async for event in _responses_stream(options, model, purpose=purpose):
            kind = str(getattr(event, "type", "") or "")
            if kind == "response.output_text.delta":
                delta = str(getattr(event, "delta", "") or "")
                if delta:
                    pieces.append(delta)
                    yield delta
            elif kind == "response.completed":
                usage = getattr(getattr(event, "response", None), "usage", None)
            elif kind in ("response.failed", "response.incomplete", "error"):
                detail = getattr(event, "message", None) or getattr(getattr(event, "response", None), "status", kind)
                raise RuntimeError(f"model stream ended with {kind}: {detail}")
    except BaseException as exc:
        error = exc
        raise
    finally:
        output = "".join(pieces)
        # Through the module, like ai_call_timer does, so a test that patches
        # pipeline_events.record_ai_call sees this row too.
        pipeline_events.record_ai_call(
            purpose=purpose,
            provider="openai",
            model=model,
            status="error" if error is not None else "ok",
            duration_ms=int((time.perf_counter() - started) * 1000),
            input_tokens=_int_or_none(getattr(usage, "input_tokens", None)),
            output_tokens=_int_or_none(getattr(usage, "output_tokens", None)),
            input_chars=len(input_text),
            output_chars=len(output),
            error_type=type(error).__name__ if error is not None else None,
            error_message=str(error) if error is not None else None,
            input_text=input_text,
            output_text=output,
        )
        if error is not None:
            generation.end(error=error)
        else:
            generation.end(output=output, usage=usage)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class ToolCall(TypedDict):
    """One function call the model asked for (Responses API ``function_call`` item)."""

    name: str
    arguments: dict
    call_id: str


async def generate_tool_calls(
    *,
    instructions: str,
    input_items: str | list[dict],
    tools: list[dict],
    tool_choice: str | dict = "required",
    max_output_tokens: int = 600,
    purpose: str | None = None,
    reasoning_effort: str | None = None,
    model: str | None = None,
) -> list[ToolCall]:
    """One model call that answers with tool calls instead of text.

    Same door as :func:`generate_text` — ``ai_call_logs`` row, Langfuse
    generation, dropped-option memory — so a decision made by tool calling is
    as observable as one made by text. ``tools`` are Responses API function
    tools (``{"type": "function", "name", "description", "parameters",
    "strict": True}``); ``input_items`` is the user text or the running list
    of items of a tool loop (earlier ``function_call`` and
    ``function_call_output`` items included). Returns the ``function_call``
    items in order; ``[]`` when the model answered with text only.

    ``model`` overrides ``settings.llm_model`` for this call only, so a caller
    that needs a different model on the same provider (the Talker of
    INTERVIA_INTERVIEW_AGENT_V2_SPEC §6.3) goes through this same door and keeps
    its ``ai_call_logs`` row and Langfuse generation.
    """
    import json

    settings = get_settings()
    purpose = purpose or caller_purpose()
    model = model or settings.llm_model
    input_text = input_items if isinstance(input_items, str) else json.dumps(input_items, ensure_ascii=False)

    generation = langfuse.Generation(
        purpose,
        model=model,
        instructions=instructions,
        input_text=input_text,
        options={
            "max_output_tokens": max_output_tokens,
            "reasoning_effort": reasoning_effort or "",
            "tools": ",".join(str(tool.get("name", "")) for tool in tools),
            "tool_choice": tool_choice if isinstance(tool_choice, str) else tool_choice.get("name", ""),
        },
    )
    usage_box: list = []

    def call() -> list[ToolCall]:
        options: dict = {
            "model": model,
            "instructions": instructions,
            "input": input_items,
            "max_output_tokens": max_output_tokens,
            "store": False,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        if reasoning_effort and model not in _NO_REASONING_MODELS:
            options["reasoning"] = {"effort": reasoning_effort}
        timer = ai_call_timer(purpose, provider="openai", model=model, input_text=input_text)
        with timer as call:
            response = _responses_create(options, model, purpose=purpose)
            calls: list[ToolCall] = []
            for item in getattr(response, "output", None) or []:
                if getattr(item, "type", None) != "function_call":
                    continue
                raw = getattr(item, "arguments", None) or "{}"
                try:
                    arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except json.JSONDecodeError:
                    arguments = {"_raw": raw}
                calls.append(ToolCall(
                    name=str(getattr(item, "name", "")),
                    arguments=arguments,
                    call_id=str(getattr(item, "call_id", "")),
                ))
            call.done(json.dumps(calls, ensure_ascii=False), getattr(response, "usage", None))
            usage_box.append(getattr(response, "usage", None))
        return calls

    try:
        calls = await run_in_threadpool(call)
    except Exception as exc:
        generation.end(error=exc)
        raise
    generation.end(output=json.dumps(calls, ensure_ascii=False), usage=usage_box[0] if usage_box else None)
    return calls


async def generate_text(
    *,
    instructions: str,
    input_text: str,
    max_output_tokens: int = 1200,
    temperature: float | None = None,
    purpose: str | None = None,
    reasoning_effort: str | None = None,
    json_schema: dict | None = None,
    model: str | None = None,
) -> str:
    """One model call. Logged to ai_call_logs (purpose defaults to the caller).

    ``reasoning_effort`` ("none", "low"…) and ``json_schema`` (Structured
    Outputs: ``{"name": …, "schema": …}``) are optional; a model or endpoint that
    refuses either is called again without it, and later calls skip it.
    ``model`` overrides ``settings.llm_model`` for this call only.
    """
    settings = get_settings()
    purpose = purpose or caller_purpose()
    model = model or settings.llm_model

    # Langfuse generation inside the current interview turn or grading run (no-op otherwise).
    generation = langfuse.Generation(
        purpose,
        model=model,
        instructions=instructions,
        input_text=input_text,
        options={
            "max_output_tokens": max_output_tokens,
            "reasoning_effort": reasoning_effort or "",
            "json_schema": (json_schema or {}).get("name", ""),
        },
    )
    usage_box: list = []

    def call() -> str:
        options: dict = {
            "model": model,
            "instructions": instructions,
            "input": input_text,
            "max_output_tokens": max_output_tokens,
            "store": False,
        }
        if temperature is not None and _takes_temperature(model):
            options["temperature"] = temperature
        if reasoning_effort and model not in _NO_REASONING_MODELS:
            options["reasoning"] = {"effort": reasoning_effort}
        if json_schema and model not in _NO_SCHEMA_MODELS:
            options["text"] = {"format": {"type": "json_schema", "strict": True, **json_schema}}
        timer = ai_call_timer(purpose, provider="openai", model=model, input_text=input_text)
        with timer as call:
            response = _responses_create(options, model, purpose=purpose)
            output = str(response.output_text or "").strip()
            call.done(output, getattr(response, "usage", None))
            usage_box.append(getattr(response, "usage", None))
        return output

    try:
        output = await run_in_threadpool(call)
    except Exception as exc:
        generation.end(error=exc)
        raise
    generation.end(output=output, usage=usage_box[0] if usage_box else None)
    return output


async def synthesize_speech(text: str) -> tuple[bytes, str]:
    if len(text) > 4096:
        raise ValueError("Speech input must not exceed 4096 characters")

    def call() -> bytes:
        response = _client().audio.speech.create(
            model="gpt-4o-mini-tts", voice="alloy", input=text, response_format="mp3"
        )
        return response.read()

    return await run_in_threadpool(call), "audio/mpeg"
