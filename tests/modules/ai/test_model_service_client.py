import httpx
import pytest

from src.modules.ai.client import GenerationRequest, ModelServiceClient, ModelServiceError
from src.modules.ai.config import ModelServiceConfig


@pytest.mark.asyncio
async def test_model_service_client_uses_stable_generate_contract() -> None:
    captured = {}

    async def json_handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"output_text": "result"})

    client = ModelServiceClient(
        ModelServiceConfig("https://model.example", "secret", 5, 100, 50),
        transport=httpx.MockTransport(json_handler),
    )
    result = await client.generate(GenerationRequest(input_text="JD", instructions="extract", max_output_tokens=99))

    assert result == "result"
    assert captured["authorization"] == "Bearer secret"
    assert captured["body"]["max_output_tokens"] == 50


@pytest.mark.asyncio
async def test_model_service_client_rejects_input_over_configured_limit() -> None:
    client = ModelServiceClient(ModelServiceConfig("https://model.example", "secret", 5, 2, 50))
    with pytest.raises(ModelServiceError, match="exceeds"):
        await client.generate(GenerationRequest(input_text="too long", instructions="extract"))
