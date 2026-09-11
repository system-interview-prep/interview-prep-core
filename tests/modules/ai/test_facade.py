from types import SimpleNamespace

import pytest

from src.modules.ai import facade


@pytest.mark.asyncio
async def test_generate_text_uses_responses_without_storage(monkeypatch) -> None:
    captured = {}

    class Responses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output_text=" answer ")

    monkeypatch.setattr(facade, "_client", lambda: SimpleNamespace(responses=Responses()))
    assert await facade.generate_text(instructions="system", input_text="hello", temperature=0.0) == "answer"
    assert captured["store"] is False
    assert captured["instructions"] == "system"
    assert captured["temperature"] == 0.0


@pytest.mark.asyncio
async def test_speech_rejects_openai_input_over_limit() -> None:
    with pytest.raises(ValueError):
        await facade.synthesize_speech("x" * 4097)
