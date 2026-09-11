"""Provider-neutral HTTP client for the project's self-hosted AI service."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from src.modules.ai.config import ModelServiceConfig, get_model_service_config


class ModelServiceError(RuntimeError):
    """A model endpoint was unavailable or returned an invalid response."""


@dataclass(frozen=True)
class GenerationRequest:
    input_text: str
    instructions: str
    max_output_tokens: int | None = None
    temperature: float = 0.0


class ModelServiceClient:
    """Call the stable ``POST /generate`` contract provided by model services."""

    def __init__(self, config: ModelServiceConfig | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._config = config or get_model_service_config()
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    async def health(self) -> dict[str, Any]:
        if not self.enabled:
            raise ModelServiceError("AI model service is not configured")
        async with httpx.AsyncClient(timeout=self._config.timeout_seconds, transport=self._transport) as client:
            response = await client.get(
                f"{self._config.base_url}/health",
                headers={"Authorization": f"Bearer {self._config.api_key}"},
            )
        if response.is_error:
            raise ModelServiceError(f"AI model service health check failed: HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, dict):
            raise ModelServiceError("AI model service returned an invalid health response")
        return data

    async def generate(self, request: GenerationRequest) -> str:
        if not self.enabled:
            raise ModelServiceError("AI model service is not configured")
        if len(request.input_text) > self._config.max_input_chars:
            raise ModelServiceError(f"AI input exceeds configured limit of {self._config.max_input_chars} characters")
        max_tokens = min(request.max_output_tokens or self._config.max_output_tokens, self._config.max_output_tokens)
        payload = {
            "input": request.input_text,
            "instructions": request.instructions,
            "max_output_tokens": max_tokens,
            "temperature": request.temperature,
        }
        headers = {"Authorization": f"Bearer {self._config.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self._config.timeout_seconds, transport=self._transport) as client:
                response = await client.post(f"{self._config.base_url}/generate", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ModelServiceError(f"AI model service request failed: {type(exc).__name__}: {exc}") from exc
        if response.is_error:
            raise ModelServiceError(f"AI model service returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise ModelServiceError("AI model service returned invalid JSON") from exc
        text = data.get("output_text") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text.strip():
            if isinstance(data, dict):
                preview = {str(key): str(value)[:300] for key, value in data.items()}
                raise ModelServiceError(f"AI model service response is missing output_text: {preview}")
            raise ModelServiceError("AI model service response is missing output_text")
        return text.strip()
