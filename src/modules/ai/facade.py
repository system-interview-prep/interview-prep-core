"""Public OpenAI adapter shared by business modules."""

from functools import lru_cache

from fastapi.concurrency import run_in_threadpool

from src.core import llm_tracing
from src.core.config import get_settings
from src.core.pipeline_events import ai_call_timer, caller_purpose

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


@lru_cache
def _client():
    from openai import OpenAI

    settings = get_settings()
    api_key = settings.openai_api_key
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    # The SDK default is a 600s read timeout with 2 retries, so a stalled
    # provider could hold an interview turn for half an hour. Callers already
    # fall back on provider errors; fail fast enough for them to do so.
    return OpenAI(api_key=api_key, timeout=settings.ai_request_timeout_seconds, max_retries=1)


async def generate_text(
    *,
    instructions: str,
    input_text: str,
    max_output_tokens: int = 1200,
    temperature: float | None = None,
    purpose: str | None = None,
    reasoning_effort: str | None = None,
    json_schema: dict | None = None,
) -> str:
    """One model call. Logged to ai_call_logs (purpose defaults to the caller).

    ``reasoning_effort`` ("none", "low"…) and ``json_schema`` (Structured
    Outputs: ``{"name": …, "schema": …}``) are optional; a model or endpoint that
    refuses either is called again without it, and later calls skip it.
    """
    settings = get_settings()
    purpose = purpose or caller_purpose()
    model = settings.llm_model

    # Langfuse generation inside the current interview turn or grading run (no-op otherwise).
    generation = llm_tracing.Generation(
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
            response = None
            for _ in range(4):
                try:
                    response = _client().responses.create(**options)
                    break
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
                    else:
                        raise
            if response is None:
                raise RuntimeError("model call failed after dropping unsupported options")
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
