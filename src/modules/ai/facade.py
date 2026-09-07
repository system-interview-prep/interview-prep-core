"""Public OpenAI adapter shared by business modules."""

from functools import lru_cache

from fastapi.concurrency import run_in_threadpool

from src.core.config import get_settings


@lru_cache
def _client():
    from openai import OpenAI

    api_key = get_settings().openai_api_key
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    return OpenAI(api_key=api_key)


async def generate_text(*, instructions: str, input_text: str, max_output_tokens: int = 1200) -> str:
    settings = get_settings()

    def call() -> str:
        response = _client().responses.create(
            model=settings.llm_model,
            instructions=instructions,
            input=input_text,
            max_output_tokens=max_output_tokens,
            store=False,
        )
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
