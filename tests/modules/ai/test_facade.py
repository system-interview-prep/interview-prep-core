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
    monkeypatch.setattr(facade.get_settings(), "llm_model", "gpt-4.1-mini")
    assert await facade.generate_text(instructions="system", input_text="hello", temperature=0.0) == "answer"
    assert captured["store"] is False
    assert captured["instructions"] == "system"
    assert captured["temperature"] == 0.0


@pytest.mark.asyncio
async def test_speech_rejects_openai_input_over_limit() -> None:
    with pytest.raises(ValueError):
        await facade.synthesize_speech("x" * 4097)


@pytest.mark.asyncio
async def test_reasoning_models_get_no_temperature_and_refused_options_are_dropped(monkeypatch) -> None:
    """25/10: a GPT-5 model refused temperature, costing one failed request per process."""
    calls: list[dict] = []

    class Responses:
        def create(self, **kwargs):
            calls.append(dict(kwargs))
            if "text" in kwargs:
                raise ValueError("Invalid schema for response_format: 'maxLength' is not permitted")
            return SimpleNamespace(output_text='{"acknowledgement": "À, vậy là cache năm phút."}')

    monkeypatch.setattr(facade, "_client", lambda: SimpleNamespace(responses=Responses()))
    monkeypatch.setattr(facade.get_settings(), "llm_model", "gpt-5.4-mini-test")
    monkeypatch.setattr(facade, "_NO_SCHEMA_MODELS", set())
    schema = {"name": "turn", "schema": {"type": "object", "properties": {}}}
    out = await facade.generate_text(
        instructions="s", input_text="i", temperature=0.2, reasoning_effort="none", json_schema=schema
    )
    assert out.startswith("{")
    assert "temperature" not in calls[0]
    assert calls[0]["reasoning"] == {"effort": "none"}
    assert calls[0]["text"]["format"]["type"] == "json_schema" and calls[0]["text"]["format"]["strict"] is True
    assert "text" not in calls[1]  # the schema was refused, the call is made again without it
    calls.clear()
    await facade.generate_text(instructions="s", input_text="i", json_schema=schema)
    assert len(calls) == 1 and "text" not in calls[0]  # remembered
