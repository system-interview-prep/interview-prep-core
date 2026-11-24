"""Shared configuration for self-hosted model services.

Business modules must not know whether this URL points to Colab, RunPod, or a
private cluster.  Provider-specific configuration stays behind this boundary.
"""
from dataclasses import dataclass

from src.core.config import get_settings


@dataclass(frozen=True)
class ModelServiceConfig:
    base_url: str | None
    api_key: str | None
    timeout_seconds: float
    max_input_chars: int
    max_output_tokens: int

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key)


def get_model_service_config() -> ModelServiceConfig:
    settings = get_settings()
    return ModelServiceConfig(
        base_url=settings.ai_model_url.rstrip("/") if settings.ai_model_url else None,
        api_key=settings.ai_service_api_key,
        timeout_seconds=settings.ai_request_timeout_seconds,
        max_input_chars=settings.ai_max_input_chars,
        max_output_tokens=settings.ai_max_output_tokens,
    )
