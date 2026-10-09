"""Public OpenAI adapter shared by business modules."""

from functools import lru_cache

from fastapi.concurrency import run_in_threadpool

from src.core.config import get_settings


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
    *, instructions: str, input_text: str, max_output_tokens: int = 1200, temperature: float | None = None
) -> str:
    settings = get_settings()

    def call() -> str:
        options = {
            "model": settings.llm_model,
            "instructions": instructions,
            "input": input_text,
            "max_output_tokens": max_output_tokens,
            "store": False,
        }
        if temperature is not None:
            options["temperature"] = temperature
        try:
            response = _client().responses.create(
                **options,
            )
        except Exception as exc:
            if "temperature" in str(exc).lower() and "temperature" in options:
                options.pop("temperature", None)
                response = _client().responses.create(
                    **options,
                )
            else:
                raise
        return str(response.output_text or "").strip()

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
