"""The text repairer the worker runs, built from settings."""

from __future__ import annotations

import logging
from pathlib import Path

from src.core.config import Settings, get_settings
from src.modules.ai.facade import CachingStructuredClient, OpenAIStructuredClient, cache_store_for
from src.modules.user_cvs.parsing.domain.text_repair import REPAIR_VERSION, TextRepairer

logger = logging.getLogger(__name__)

_CACHE_DIR = Path("models_cache/text_repair_cache")


def build_text_repairer(settings: Settings | None = None) -> TextRepairer | None:
    """``None`` when the repair is off or cannot reach a model; parsing then runs as before."""
    settings = settings or get_settings()
    if not settings.cv_text_repair_enabled or not settings.openai_api_key:
        return None
    model = settings.matching_judge_model or settings.llm_model
    try:
        client = OpenAIStructuredClient(
            api_key=settings.openai_api_key,
            model=model,
            purpose="cv.text_repair",
            schema_name="cv_text_repair",
            max_output_tokens=settings.cv_parser_max_output_tokens,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("CV text repair unavailable: %s", type(exc).__name__)
        return None
    cached = CachingStructuredClient(
        client,
        model=model,
        prompt_version=REPAIR_VERSION,
        store=cache_store_for("text_repair", _CACHE_DIR, settings),
    )
    return TextRepairer(cached)
