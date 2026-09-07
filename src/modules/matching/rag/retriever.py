"""Retrieval engine for finding relevant Interview Documents.

This module provides RAG document retrieval based on topic, difficulty, history, and query texts.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from src.modules.matching.rag.embedding import build_embedding_adapter_from_env
from src.modules.matching.rag.vector_insert import VectorStoreAdapter

logger = logging.getLogger(__name__)


class Retriever:
    """Manages vector search and metadata filtering for interview documents."""

    def __init__(self, adapter: Optional[VectorStoreAdapter] = None):
        self.vector_store = adapter or VectorStoreAdapter()
        self._embedding_adapter = None

    def _get_embedding_adapter(self):
        if self._embedding_adapter is None:
            self._embedding_adapter = build_embedding_adapter_from_env()
        return self._embedding_adapter

    def retrieve(
        self,
        *,
        topic: Optional[str] = None,
        difficulty: Optional[str] = None,
        history: Optional[List[str]] = None,
        query_text: Optional[str] = None,
        query_vector: Optional[List[float]] = None,
        k: int = 5,
    ) -> List[Dict[str, Any]]:
        """Retrieve relevant documents based on filters and vector similarity.

        Args:
            topic: Topic string filter (e.g. 'concurrency').
            difficulty: Difficulty level filter (e.g. 'intermediate').
            history: List of chunk_ids or knowledge_unit_id strings to filter out.
            query_text: Raw text to generate embedding for vector search.
            query_vector: Precomputed query embedding vector.
            k: Number of documents to return.

        Returns:
            List of matching records: [{"id": ..., "text": ..., "metadata": ..., "score": ...}]
        """
        # Ensure we have a search vector
        if query_vector is None:
            if query_text:
                embedder = self._get_embedding_adapter()
                query_vector = embedder.embed_texts([query_text])[0]
            elif topic:
                embedder = self._get_embedding_adapter()
                query_vector = embedder.embed_texts([topic])[0]
            else:
                dim = int(os.getenv("EMBEDDING_DIMENSION", "384"))
                query_vector = [0.0] * dim

        # Build filters
        filters: Dict[str, Any] = {"is_active": True}
        if topic:
            filters["topic"] = topic
        if difficulty:
            filters["difficulty"] = difficulty

        history_list = history or []
        search_limit = max(k * 3, k + len(history_list), 50)

        candidates = self.vector_store.search(
            query_vector=query_vector,
            limit=search_limit,
            filters=filters,
        )

        results: List[Dict[str, Any]] = []
        for cand in candidates:
            chunk_id = cand.get("id")
            meta = cand.get("metadata", {})
            ku_id = meta.get("knowledge_unit_id")

            # Filter out chunks already in history (by chunk_id or knowledge_unit_id)
            if chunk_id in history_list or ku_id in history_list:
                logger.info("Retriever filtered out already asked chunk/KU: %s", chunk_id)
                continue

            results.append(cand)
            if len(results) >= k:
                break

        return results

