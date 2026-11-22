"""Content-keyed cache for judge calls.

Same reason as the evidence-search cache: the model is not deterministic at
the margin, and two runs of an unchanged pair must not disagree because of
it.  The key covers the prompt version and model as well as the input, so a
prompt change never serves an old reading.  Every failure degrades to calling
the client.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from src.modules.matching.judge.grounded_judge import JudgeClient

logger = logging.getLogger(__name__)

_CACHE_DIR = Path("models_cache/judge_cache")


class CachingJudgeClient:
    """Wrap a ``JudgeClient`` so one distinct input is answered once."""

    def __init__(self, inner: JudgeClient, *, model: str, prompt_version: str, directory: Path | None = None):
        self._inner = inner
        self._model = model
        self._prompt_version = prompt_version
        self._directory = directory or _CACHE_DIR

    def _key(self, instructions: str, input_text: str) -> str:
        material = json.dumps(
            {
                "prompt_version": self._prompt_version,
                "model": self._model,
                "instructions": instructions,
                "input": input_text,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def complete(self, *, instructions: str, input_text: str, output_schema: dict[str, Any]) -> str:
        path = self._directory / f"{self._key(instructions, input_text)}.json"
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            logger.warning("judge cache unreadable, recomputing: %s", path.name)
        text = self._inner.complete(
            instructions=instructions, input_text=input_text, output_schema=output_schema
        )
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except Exception:  # noqa: BLE001
            logger.warning("judge cache unwritable: %s", path.name)
        return text
