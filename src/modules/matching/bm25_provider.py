"""BM25 retrieval and scoring providers for CV-JD matching.

Supports:
1. In-memory Okapi BM25 (pure Python, offline, zero-dependency).
2. ParadeDB BM25 (`pg_search` Tantivy index on PostgreSQL `user_cvs`),
   with corpus-level IDF, saturation normalization into [0, 1], and
   automatic in-memory fallback.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import text

from src.modules.matching.bm25 import bm25_similarity, tokenize_text

logger = logging.getLogger(__name__)

DEFAULT_SATURATION_K = 15.0


@runtime_checkable
class Bm25Provider(Protocol):
    """Protocol for BM25 score calculation between a JD (query) and CV (document)."""

    def score(self, query_text: str, doc_text: str, doc_id: str | None = None) -> float:
        """Compute normalized BM25 similarity score in range [0.0, 1.0]."""
        ...

    async def score_async(self, query_text: str, doc_text: str, doc_id: str | None = None) -> float:
        """Compute a score without leaving the caller's event loop."""
        ...


class InMemoryBm25Provider:
    """Computes BM25 similarity purely in memory without external database."""

    def score(self, query_text: str, doc_text: str, doc_id: str | None = None) -> float:
        del doc_id  # Unused in purely in-memory evaluation
        return bm25_similarity(query_text, doc_text)

    async def score_async(self, query_text: str, doc_text: str, doc_id: str | None = None) -> float:
        return self.score(query_text, doc_text, doc_id)


class ParadeDbBm25Provider:
    """Queries ParadeDB's Tantivy-powered BM25 index on PostgreSQL `user_cvs`.

    Uses `raw_text @@@ :query` with `pdb.score(id)` for corpus-wide IDF calculation,
    then normalizes the score to [0, 1] using saturation function:
        S_norm = raw_score / (raw_score + saturation_k)
    """

    def __init__(
        self,
        session_factory: Any = None,
        saturation_k: float = DEFAULT_SATURATION_K,
        fallback: Bm25Provider | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.saturation_k = max(0.1, saturation_k)
        self.fallback = fallback or InMemoryBm25Provider()

    @staticmethod
    def sanitize_query(query_text: str, max_terms: int = 100) -> str:
        """Extract clean alphanumeric and technical terms safe for ParadeDB Tantivy query."""
        tokens = tokenize_text(query_text)
        # Escape any Tantivy special characters if present in tokens
        safe_tokens: list[str] = []
        for tok in tokens[:max_terms]:
            # Clean non-alphanumeric punctuation except safe chars (+, #, ., -)
            cleaned = re.sub(r"[^\w+#.-]", "", tok, flags=re.UNICODE)
            if cleaned:
                safe_tokens.append(cleaned)
        return " ".join(safe_tokens)

    async def score_async(self, query_text: str, doc_text: str, doc_id: str | None = None) -> float:
        """Asynchronously query ParadeDB BM25 score from user_cvs table."""
        if not doc_id:
            return self.fallback.score(query_text, doc_text, doc_id)

        clean_query = self.sanitize_query(query_text)
        if not clean_query:
            return 0.0

        if self.session_factory is None:
            from src.infrastructure.database import SessionFactory

            session_factory = SessionFactory
        else:
            session_factory = self.session_factory

        try:
            sql = text(
                """
                SELECT pdb.score(id) AS bm25_score
                FROM user_cvs
                WHERE id = :cv_id AND raw_text @@@ :query
                LIMIT 1
                """
            )
            async with session_factory() as session:
                result = await session.execute(sql, {"cv_id": doc_id, "query": clean_query})
                row = result.mappings().one_or_none()
                if row is None or row.get("bm25_score") is None:
                    # Document did not match query keywords in ParadeDB
                    return 0.0
                raw_score = float(row["bm25_score"])
                # Saturation normalization: maps [0, +inf) -> [0, 1)
                normalized = raw_score / (raw_score + self.saturation_k)
                return min(1.0, max(0.0, round(normalized, 4)))
        except Exception as exc:
            logger.warning(
                "ParadeDB BM25 query failed for doc_id=%s; falling back to in-memory: %s",
                doc_id,
                exc,
            )
            return self.fallback.score(query_text, doc_text, doc_id)

    def score(self, query_text: str, doc_text: str, doc_id: str | None = None) -> float:
        """Compatibility wrapper for synchronous callers outside an event loop.

        FastAPI request handling must use :meth:`score_async`; an asyncpg pool
        cannot safely be moved to an event loop created by ``asyncio.run``.
        """
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None and loop.is_running():
                raise RuntimeError("score() cannot be used inside a running event loop; await score_async()")
            return asyncio.run(self.score_async(query_text, doc_text, doc_id))
        except Exception as exc:
            logger.warning("ParadeDB runner failed; falling back to in-memory: %s", exc)
            return self.fallback.score(query_text, doc_text, doc_id)


def get_bm25_provider(mode: str = "auto") -> Bm25Provider:
    """Factory to get the appropriate BM25 provider based on configuration."""
    if mode == "in_memory":
        return InMemoryBm25Provider()
    if mode == "paradedb":
        return ParadeDbBm25Provider()
    # Default 'auto': ParadeDbBm25Provider with graceful in-memory fallback
    return ParadeDbBm25Provider()
