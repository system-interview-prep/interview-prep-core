"""Content-keyed cache for judge calls.

The cache itself is ``src.modules.ai``'s ``CachingStructuredClient``; this
module fixes the judge's purpose and file directory and keeps the name its
callers use.  Where answers are kept (files or the ``llm_call_cache`` table)
follows ``Settings.llm_cache_backend``.
"""

from __future__ import annotations

from pathlib import Path

from src.modules.ai.facade import CacheStore, CachingStructuredClient, StructuredClient, cache_store_for

_CACHE_DIR = Path("models_cache/judge_cache")


class CachingJudgeClient(CachingStructuredClient):
    """Wrap a ``JudgeClient`` so one distinct input is answered once."""

    def __init__(
        self,
        inner: StructuredClient,
        *,
        model: str,
        prompt_version: str,
        directory: Path | None = None,
        store: CacheStore | None = None,
    ) -> None:
        super().__init__(
            inner,
            model=model,
            prompt_version=prompt_version,
            store=store or cache_store_for("judge", directory or _CACHE_DIR),
        )
