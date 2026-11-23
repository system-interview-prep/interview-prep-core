"""Admin "Danh sách mô hình AI" (src/modules/admin/ai/ai_overview.py).

Chi phí và tóm tắt đánh giá đã chuyển sang src/infrastructure/langfuse/;
test của chúng ở tests/infrastructure/test_langfuse_metrics.py và _summary.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest

from src.modules.admin import ai_overview


def test_configured_models_follow_the_settings(monkeypatch) -> None:
    monkeypatch.setenv("LIVEKIT_STT_MODEL", "assemblyai/universal-3-6-pro")
    monkeypatch.setenv("VOICE_LAB_TTS_PROVIDER", "vieneu")
    rows = {row["capability"]: row for row in ai_overview.configured_models()}
    llm = ai_overview.get_settings().llm_model
    assert rows["INTERVIEW_TURN"]["model"] == llm and rows["INTERVIEW_TURN"]["provider"] == "openai"
    assert rows["INTERVIEW_SCORING"]["model"] == llm
    assert (rows["VOICE_STT"]["provider"], rows["VOICE_STT"]["model"]) == ("assemblyai", "universal-3-6-pro")
    assert rows["VOICE_TTS"]["provider"] == "vieneu"
    assert all("gemini" not in str(row["model"]).casefold() for row in rows.values())
