"""Content-keyed cache for judge calls.

The cache itself is ``src.modules.ai.facade.CachingStructuredClient``; this
module fixes the judge's directory and keeps the name its callers use.
"""

from __future__ import annotations

from pathlib import Path

from src.modules.ai.facade import CachingStructuredClient, StructuredClient

_CACHE_DIR = Path("models_cache/judge_cache")


class CachingJudgeClient(CachingStructuredClient):
    """Wrap a ``JudgeClient`` so one distinct input is answered once."""

    def __init__(
        self, inner: StructuredClient, *, model: str, prompt_version: str, directory: Path | None = None
    ) -> None:
        super().__init__(
            inner, model=model, prompt_version=prompt_version, directory=directory or _CACHE_DIR
        )
