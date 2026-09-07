"""OpenAI integration for question generation, evaluation, follow-up, and feedback."""

from __future__ import annotations

import os


class LLMService:
    """Wrapper service for executing prompts through the OpenAI Responses API."""

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
        response = self._client.responses.create(model=self.model, input=prompt, store=False)
        return response.output_text
