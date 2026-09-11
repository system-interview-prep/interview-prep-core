"""Shared AI configuration, clients, prompts and model policies."""

from src.modules.ai.client import GenerationRequest, ModelServiceClient, ModelServiceError
from src.modules.ai.config import ModelServiceConfig, get_model_service_config
from src.modules.ai.facade import generate_text, synthesize_speech

__all__ = [
    "GenerationRequest", "ModelServiceClient", "ModelServiceConfig", "ModelServiceError",
    "generate_text", "get_model_service_config", "synthesize_speech",
]
