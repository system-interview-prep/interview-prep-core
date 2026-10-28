"""Admin "Cài đặt": the configuration the backend is running with (read-only).

Secrets are never returned: a key is reported only as set / not set.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.core import llm_tracing
from src.core.config import get_settings
from src.modules.interviews.facade import interview_config_groups, interview_config_item


def config_view() -> dict[str, Any]:
    s = get_settings()
    item = interview_config_item
    groups = interview_config_groups()
    groups.append(
        {
            "key": "documents",
            "items": [
                item("cvParserMode", s.cv_parser_mode, "CV_PARSER_MODE"),
                item("jdParserMode", s.jd_parser_mode, "JD_PARSER_MODE"),
                item("ocrProvider", s.ocr_provider, "OCR_PROVIDER"),
                item("embeddingModel", s.embedding_model, "EMBEDDING_MODEL"),
            ],
        }
    )
    groups.append(
        {
            "key": "monitoring",
            "items": [
                item("langfuseEnabled", llm_tracing.enabled(), "LANGFUSE_PUBLIC_KEY"),
                item("langfuseHost", s.langfuse_base_url, "LANGFUSE_BASE_URL"),
                item("langfuseTextSampleRate", s.langfuse_text_sample_rate, "LANGFUSE_TEXT_SAMPLE_RATE", 0.2),
                item("langfuseReviewQueue", bool(s.langfuse_review_queue_id), "LANGFUSE_REVIEW_QUEUE_ID"),
                item(
                    "langfuseReviewRandomRate",
                    s.langfuse_review_random_rate,
                    "LANGFUSE_REVIEW_RANDOM_RATE",
                    0.05,
                ),
            ],
        }
    )
    groups.append(
        {
            "key": "system",
            "items": [
                item("appEnv", s.app_env, "APP_ENV"),
                item("openaiKey", bool(s.openai_api_key), "OPENAI_API_KEY"),
            ],
        }
    )
    return {"groups": groups, "generatedAt": datetime.now(UTC).isoformat()}
