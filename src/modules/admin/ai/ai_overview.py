"""Admin "Danh sách mô hình AI" and "Đánh giá AI" pages.

* ``models_view``: which provider and model each AI feature uses, read from the
  backend's configuration (what will be used), next to what ``ai_call_logs``
  recorded in the last 30 days (what was actually used). Before 25/10 the page
  showed a hard-coded Gemini list that the backend never used.
* ``cost_view``: LLM cost as Langfuse prices it (Metrics API): total, per model,
  per feature, per day. Only calls traced to Langfuse are priced (interview
  turns, grading, judges, experiments), not OCR, embeddings or voice.
* ``evaluation_view``: a summary of the Langfuse project (LLM monitoring design,
  tiers 2–4): judge and reviewer pass rates, flagged turns, the review queue,
  the golden-set experiment runs, with links into Langfuse. Datasets,
  experiments, regressions and prompt versions live in Langfuse itself; the
  former placeholder pages for them redirect here.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.langfuse import metrics as lf_metrics
from src.infrastructure.langfuse import summary as lf_summary
from src.core.config import get_settings
from src.modules.admin.ai.repository import observed_models



# --------------------------------------------------------------------------- models


def configured_models() -> list[dict[str, Any]]:
    """Each AI feature with the provider and model the configuration selects."""
    s = get_settings()
    llm = {"provider": s.llm_provider or "openai", "model": s.llm_model, "source": "LLM_MODEL"}
    stt = os.getenv("LIVEKIT_STT_MODEL", "assemblyai/universal-3-6-pro").strip()
    stt_provider, _, stt_model = stt.partition("/")
    tts_provider = os.getenv("VOICE_LAB_TTS_PROVIDER", "elevenlabs").strip().lower()
    tts_model = {
        "vieneu": os.getenv("VIENEU_TTS_MODEL", "") or "VieNeu (máy chủ riêng)",
        "elevenlabs": os.getenv("ELEVENLABS_TTS_MODEL", "") or "ElevenLabs",
        "cartesia": os.getenv("CARTESIA_TTS_MODEL", "") or "Cartesia",
    }.get(tts_provider, tts_provider)
    ocr_model = s.mineru_model_version if s.ocr_provider == "mineru" else s.paddleocr_model
    rows: list[dict[str, Any]] = [
        {"capability": "INTERVIEW_TURN", **llm, "active": True},
        {"capability": "QUESTION_PREP", **llm, "active": True},
        {"capability": "INTERVIEW_SCORING", **llm, "active": True},
        {
            "capability": "VOICE_STT",
            "provider": stt_provider if stt_model else "livekit",
            "model": stt_model or stt,
            "source": "LIVEKIT_STT_MODEL",
            "active": True,
        },
        {
            "capability": "VOICE_TTS",
            "provider": tts_provider,
            "model": tts_model,
            "source": "VOICE_LAB_TTS_PROVIDER",
            "active": True,
        },
        {
            "capability": "DOCUMENT_OCR",
            "provider": s.ocr_provider,
            "model": ocr_model,
            "source": "OCR_PROVIDER",
            "active": True,
        },
        {
            "capability": "CV_PARSING",
            **llm,
            "source": "CV_PARSER_MODE",
            "mode": s.cv_parser_mode,
            "active": s.cv_parser_mode.casefold() == "hybrid",
        },
        {
            "capability": "JD_PARSING",
            **llm,
            "source": "JD_PARSER_MODE",
            "mode": s.jd_parser_mode,
            "active": s.jd_parser_mode.casefold() == "hybrid",
        },
        {
            "capability": "MATCHING_EMBEDDING",
            "provider": s.embedding_provider,
            "model": s.embedding_model,
            "source": "EMBEDDING_MODEL",
            "active": True,
        },
        {
            "capability": "MATCHING_EVIDENCE",
            **llm,
            "source": "MATCHING_LLM_EVIDENCE_SEARCH_ENABLED",
            "active": bool(s.matching_llm_evidence_search_enabled and s.openai_api_key),
        },
        {
            "capability": "MATCHING_CLARIFICATION",
            "provider": "openai",
            "model": s.matching_clarification_model,
            "source": "MATCHING_CLARIFICATION_MODEL",
            "active": bool(s.matching_clarification_questions_enabled),
        },
        {
            "capability": "MATCHING_JEV",
            "provider": "typesafe",
            "model": s.typesafe_default_model,
            "source": "JEV_CLARIFICATION_ENABLED",
            "active": bool(s.jev_clarification_enabled and s.typesafe_api_key),
        },
    ]
    return rows


async def models_view(db: AsyncSession, *, days: int = 30) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    try:
        result = await observed_models(db, since=since)
        observed = [
            {
                "purpose": row["purpose"],
                "provider": row["provider"],
                "model": row["model"],
                "calls": int(row["calls"]),
                "errors": int(row["errors"]),
                "lastAt": row["last_at"].isoformat() if row.get("last_at") else None,
            }
            for row in result
        ]
    except Exception:  # noqa: BLE001 - ai_call_logs missing before migration 0029
        await db.rollback()
        observed = []
    return {
        "windowDays": days,
        "configured": configured_models(),
        "observed": observed,
        "generatedAt": datetime.now(UTC).isoformat(),
    }


# --------------------------------------------------------------------------- Langfuse
#
# The reads themselves live in src/infrastructure/langfuse/: cost in metrics.py,
# the evaluation summary in summary.py. This page only forwards.


async def cost_view(*, days: int = 7, refresh: bool = False) -> dict[str, Any]:
    return await lf_metrics.cost_view(days=days, refresh=refresh)


async def evaluation_view(*, days: int = 7, refresh: bool = False) -> dict[str, Any]:
    return await lf_summary.evaluation_view(days=days, refresh=refresh)
