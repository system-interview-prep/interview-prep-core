"""BM25 retrieval and scoring providers for CV-JD matching.

Production scores with ParadeDB BM25 (`pg_search` Tantivy index on PostgreSQL
`user_cvs`): corpus-level IDF, saturation normalization into [0, 1].

`InMemoryBm25Provider` is a different formula, not a second implementation of
the same one - no IDF, term weight by token length, coverage-ratio
normalization. Measured on 100 real CV-JD pairs it reads about 4.7x lower
(mean 0.087 against 0.409) and picks a different top-ranked CV on 4 of 10 JDs.
It therefore must never stand in for ParadeDB behind the caller's back; it
exists for offline tests and local development that have no index.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import text

from src.modules.matching.retrieval.bm25 import bm25_similarity, tokenize_text

logger = logging.getLogger(__name__)

DEFAULT_SATURATION_K = 15.0

# Which provider actually produced the last sparse score (for the admin matching
# view). A shared dict, so a score computed inside asyncio.run still reaches it.
_provider_used: ContextVar[dict[str, str] | None] = ContextVar("bm25_provider_used", default=None)


def _note_provider(name: str) -> None:
    holder = _provider_used.get()
    if holder is not None:
        holder["provider"] = name


@contextmanager
def track_bm25_provider() -> Iterator[dict[str, str]]:
    """``with track_bm25_provider() as used: ...; used.get("provider")``."""
    holder: dict[str, str] = {}
    token = _provider_used.set(holder)
    try:
        yield holder
    finally:
        _provider_used.reset(token)


@runtime_checkable
class Bm25Provider(Protocol):
    """Protocol for BM25 score calculation between a JD (query) and CV (document)."""

    def score(self, query_text: str, doc_text: str, doc_id: str | None = None) -> float | None:
        """Normalized BM25 similarity in [0.0, 1.0], or None when it cannot be assessed.

        None and 0.0 are different answers. 0.0 means the document was searched
        and shares nothing with the query. None means no search happened - the
        document is not in the index, or the index could not be reached - and
        the caller must not treat that as evidence of a poor match.
        """
        ...

    async def score_async(self, query_text: str, doc_text: str, doc_id: str | None = None) -> float | None:
        """Compute a score without leaving the caller's event loop."""
        ...


class InMemoryBm25Provider:
    """Computes BM25 similarity purely in memory without external database."""

    def score(self, query_text: str, doc_text: str, doc_id: str | None = None) -> float:
        del doc_id  # Unused in purely in-memory evaluation
        _note_provider("in_memory")
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
    ) -> None:
        self.session_factory = session_factory
        self.saturation_k = max(0.1, saturation_k)

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

    async def score_async(
        self, query_text: str, doc_text: str, doc_id: str | None = None
    ) -> float | None:
        """ParadeDB BM25 for one stored CV, or None when it cannot be assessed.

        ``doc_text`` is deliberately unused: the score comes from the indexed
        ``user_cvs.raw_text``, because corpus IDF is only defined over the
        indexed corpus. There is no in-memory fallback - that scorer uses a
        different formula, so substituting it would silently change the scale.
        """
        del doc_text  # scored from the index, not from the caller's copy
        if not doc_id:
            _note_provider("paradedb_unavailable")
            return None

        clean_query = self.sanitize_query(query_text)
        if not clean_query:
            _note_provider("paradedb_unavailable")
            return None

        if self.session_factory is None:
            from src.infrastructure.database import SessionFactory

            session_factory = SessionFactory
        else:
            session_factory = self.session_factory

        try:
            # One round trip answers both questions: is this CV searchable at
            # all, and did it match. Without the first, a CV whose raw_text was
            # never stored scores 0.0 and reads as a confident "nothing in
            # common" when nothing was ever searched.
            sql = text(
                """
                SELECT
                    EXISTS (
                        SELECT 1 FROM user_cvs
                        WHERE id = :cv_id AND raw_text IS NOT NULL AND raw_text <> ''
                    ) AS indexed,
                    (
                        SELECT pdb.score(id) FROM user_cvs
                        WHERE id = :cv_id AND raw_text @@@ :query
                        LIMIT 1
                    ) AS bm25_score
                """
            )
            async with session_factory() as session:
                result = await session.execute(sql, {"cv_id": doc_id, "query": clean_query})
                row = result.mappings().one_or_none()
                if row is None or not row.get("indexed"):
                    logger.info("ParadeDB has no indexed text for doc_id=%s", doc_id)
                    _note_provider("paradedb_not_indexed")
                    return None
                _note_provider("paradedb")
                if row.get("bm25_score") is None:
                    # Searched, and the CV shares no query term: a real zero.
                    return 0.0
                raw_score = float(row["bm25_score"])
                # Saturation normalization: maps [0, +inf) -> [0, 1)
                normalized = raw_score / (raw_score + self.saturation_k)
                return min(1.0, max(0.0, round(normalized, 4)))
        except Exception as exc:
            logger.warning("ParadeDB BM25 query failed for doc_id=%s: %s", doc_id, exc)
            _note_provider("paradedb_unavailable")
            return None

    def score(self, query_text: str, doc_text: str, doc_id: str | None = None) -> float | None:
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
            provider = self
            if self.session_factory is None:
                # asyncio.run creates a new loop: never touch the server's pool from it.
                from src.infrastructure.database import StandaloneSessionFactory

                provider = ParadeDbBm25Provider(
                    session_factory=StandaloneSessionFactory,
                    saturation_k=self.saturation_k,
                )
            return asyncio.run(provider.score_async(query_text, doc_text, doc_id))
        except Exception as exc:
            logger.warning("ParadeDB runner failed for doc_id=%s: %s", doc_id, exc)
            _note_provider("paradedb_unavailable")
            return None


def get_bm25_provider(mode: str = "auto") -> Bm25Provider:
    """Factory to get the appropriate BM25 provider based on configuration."""
    if mode == "in_memory":
        return InMemoryBm25Provider()
    if mode == "paradedb":
        return ParadeDbBm25Provider()
    # Default 'auto': ParadeDB. It has no fallback scorer - when the index
    # cannot answer, matching reports the semantic factor as unknown rather
    # than quoting a number from a formula that is not on the same scale.
    return ParadeDbBm25Provider()
