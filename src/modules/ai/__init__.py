"""LLM provider abstraction, prompts and model policies module."""

from src.modules.ai.facade import generate_text, synthesize_speech

__all__ = ["generate_text", "synthesize_speech"]
