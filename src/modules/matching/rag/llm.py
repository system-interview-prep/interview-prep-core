"""OpenAI text-generation adapter for the matching/RAG module."""

from __future__ import annotations

import os
from src.core.pipeline_events import ai_call_timer


class LLMService:
    """Wrapper for executing a caller-owned prompt through OpenAI Responses."""

    def __init__(self) -> None:
        self.provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
        if self.provider != "openai":
            raise ValueError(f"Invalid LLM_PROVIDER: '{self.provider}'. Only 'openai' is supported.")

        from openai import OpenAI

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required.")
        self._client = OpenAI(api_key=api_key)
        self.model = os.getenv("LLM_MODEL", "gpt-5.4-mini").strip() or "gpt-5.4-mini"

    def generate_text(self, prompt: str) -> str:
        """Execute a prompt and return its text output."""
        with ai_call_timer("matching.rag.generate_text", provider="openai", model=self.model) as _ai_call:
            response = self._client.responses.create(model=self.model, input=prompt, store=False)
            _ai_call.done(response.output_text, getattr(response, "usage", None))
        return response.output_text
