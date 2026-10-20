"""Public OpenAI adapter shared by business modules."""

from functools import lru_cache

from fastapi.concurrency import run_in_threadpool

from src.core.config import get_settings
from src.core.pipeline_events import ai_call_timer, caller_purpose

# Models that answered "temperature is not supported" once. Every later call
# skips the parameter instead of paying for a rejected request first.
_NO_TEMPERATURE_MODELS: set[str] = set()


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
) -> str:
    """One model call. Logged to ai_call_logs (purpose defaults to the caller)."""
    settings = get_settings()
    purpose = purpose or caller_purpose()

    def call() -> str:
        options = {
            "model": settings.llm_model,
            "instructions": instructions,
            "input": input_text,
            "max_output_tokens": max_output_tokens,
            "store": False,
        }
        if temperature is not None and settings.llm_model not in _NO_TEMPERATURE_MODELS:
            options["temperature"] = temperature
        timer = ai_call_timer(purpose, provider="openai", model=settings.llm_model, input_text=input_text)
        with timer as call:
            try:
                response = _client().responses.create(
                    **options,
                )
            except Exception as exc:
                if "temperature" in str(exc).lower() and "temperature" in options:
                    _NO_TEMPERATURE_MODELS.add(settings.llm_model)
                    options.pop("temperature", None)
                    response = _client().responses.create(
                        **options,
                    )
                else:
                    raise
            output = str(response.output_text or "").strip()
            call.done(output, getattr(response, "usage", None))
        return output

    return await run_in_threadpool(call)


async def synthesize_speech(text: str) -> tuple[bytes, str]:
    if len(text) > 4096:
        raise ValueError("Speech input must not exceed 4096 characters")

    def call() -> bytes:
        response = _client().audio.speech.create(
            model="gpt-4o-mini-tts", voice="alloy", input=text, response_format="mp3"
        )
        return response.read()

    return await run_in_threadpool(call), "audio/mpeg"
