"""Post-retrieval data quality control layer before LLM consumption.

Filters duplicates, validates topics/difficulty, and re-ranks retrieved chunks.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class QualityLayer:
    """Post-retrieval data quality control layer for interview documents.

    Filters duplicates, aligns topics, verifies difficulty, and performs re-ranking.
    """

    def __init__(
        self,
        *,
        similarity_weight: float = 0.7,
        quality_weight: float = 0.3,
    ):
        """Initialize the quality layer.

        Args:
            similarity_weight: Weight of vector similarity score in re-ranking (0.0 to 1.0).
            quality_weight: Weight of document intrinsic quality score in re-ranking (0.0 to 1.0).
        """
        self.w_similarity = similarity_weight
        self.w_quality = quality_weight

    def filter_duplicates(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove duplicate chunks by ID or exact text content.

        Keeps the occurrence with the highest similarity score or quality score.

        Args:
            chunks: List of retrieved chunks.

        Returns:
            List of unique chunks.
        """
        unique_chunks: Dict[str, Dict[str, Any]] = {}
        seen_texts: Dict[str, str] = {}  # text -> chunk_id

        for chunk in chunks:
            chunk_id = chunk.get("id") or chunk.get("metadata", {}).get("chunk_id") or ""
            text = (chunk.get("text") or "").strip()
            similarity_score = float(chunk.get("score") or 0.0)
            quality_score = float(chunk.get("metadata", {}).get("quality_score") or 0.8)
            score = similarity_score * self.w_similarity + quality_score * self.w_quality

            if not chunk_id or not text:
                continue

            # Check for duplicate by ID
            if chunk_id in unique_chunks:
                existing = unique_chunks[chunk_id]
                exist_similarity = float(existing.get("score") or 0.0)
                exist_quality = float(existing.get("metadata", {}).get("quality_score") or 0.8)
                exist_score = exist_similarity * self.w_similarity + exist_quality * self.w_quality
                if score > exist_score:
                    unique_chunks[chunk_id] = chunk
                continue

            # Check for duplicate by text content
            if text in seen_texts:
                dup_id = seen_texts[text]
                existing = unique_chunks[dup_id]
                exist_similarity = float(existing.get("score") or 0.0)
                exist_quality = float(existing.get("metadata", {}).get("quality_score") or 0.8)
                exist_score = exist_similarity * self.w_similarity + exist_quality * self.w_quality
                if score > exist_score:
                    # Remove the old one, map text to the new chunk
                    del unique_chunks[dup_id]
                    unique_chunks[chunk_id] = chunk
                    seen_texts[text] = chunk_id
                continue

            # New unique chunk
            unique_chunks[chunk_id] = chunk
            seen_texts[text] = chunk_id

        return list(unique_chunks.values())

    def filter_topic(
        self, chunks: List[Dict[str, Any]], target_topic: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Keep only chunks whose topic metadata matches target_topic (case-insensitive).

        Args:
            chunks: List of chunks.
            target_topic: Target topic to filter by. If None, no filtering is done.

        Returns:
            Filtered list of chunks.
        """
        if not target_topic:
            return chunks

        target_clean = target_topic.strip().lower()
        filtered = []
        for chunk in chunks:
            topic = chunk.get("metadata", {}).get("topic") or ""
            if topic.strip().lower() == target_clean:
                filtered.append(chunk)
            else:
                logger.info(
                    "QualityLayer filtered out chunk %s due to topic mismatch (expected: %s, got: %s)",
                    chunk.get("id"),
                    target_topic,
                    topic,
                )
        return filtered

    def filter_difficulty(
        self, chunks: List[Dict[str, Any]], target_difficulty: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Keep only chunks whose difficulty matches target_difficulty (case-insensitive).

        Args:
            chunks: List of chunks.
            target_difficulty: Target difficulty. If None, no filtering is done.

        Returns:
            Filtered list of chunks.
        """
        if not target_difficulty:
            return chunks

        target_clean = target_difficulty.strip().lower()
        filtered = []
        for chunk in chunks:
            diff = chunk.get("metadata", {}).get("difficulty") or ""
            if diff.strip().lower() == target_clean:
                filtered.append(chunk)
            else:
                logger.info(
                    "QualityLayer filtered out chunk %s due to difficulty mismatch (expected: %s, got: %s)",
                    chunk.get("id"),
                    target_difficulty,
                    diff,
                )
        return filtered

    def rank_chunks(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Rank chunks by a combined score: similarity_score * w_similarity + quality_score * w_quality.

        Adds a "combined_quality_score" field to each chunk's metadata for transparency.

        Args:
            chunks: List of chunks.

        Returns:
            Re-ranked list of chunks sorted descending by score.
        """
        ranked = []
        for chunk in chunks:
            similarity_score = float(chunk.get("score") or 0.0)
            quality_score = float(chunk.get("metadata", {}).get("quality_score") or 0.8)
            combined = similarity_score * self.w_similarity + quality_score * self.w_quality

            # Clone chunk to avoid modifying original in-place destructively
            new_chunk = dict(chunk)
            new_chunk["score"] = combined
            if "metadata" in new_chunk:
                new_chunk["metadata"] = dict(new_chunk["metadata"])
                new_chunk["metadata"]["combined_quality_score"] = combined
                new_chunk["metadata"]["raw_similarity_score"] = similarity_score

            ranked.append(new_chunk)

        ranked.sort(key=lambda x: x["score"], reverse=True)
        return ranked

    def select_follow_ups(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Select chunks of type 'follow_up'.

        Args:
            chunks: List of chunks.

        Returns:
            Filtered list of follow-up chunks.
        """
        return [
            chunk
            for chunk in chunks
            if chunk.get("metadata", {}).get("chunk_type") == "follow_up"
        ]

    def select_deliverables(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Select chunks of type 'deliverables'.

        Args:
            chunks: List of chunks.

        Returns:
            Filtered list of deliverables chunks.
        """
        return [
            chunk
            for chunk in chunks
            if chunk.get("metadata", {}).get("chunk_type") == "deliverables"
        ]

    def process(
        self,
        chunks: List[Dict[str, Any]],
        *,
        topic: Optional[str] = None,
        difficulty: Optional[str] = None,
        k: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Process retrieved chunks through the full quality pipeline.

        Args:
            chunks: Raw retrieved chunks.
            topic: Optional target topic filter.
            difficulty: Optional target difficulty filter.
            k: Maximum number of final ranked chunks to return.

        Returns:
            Dict containing quality layers:
                - 'ranked_chunks': Cleaned and re-ranked list of chunks.
                - 'follow_ups': Extracted follow-up question/material chunks.
                - 'deliverables': Extracted deliverable requirements chunks.
                - 'metadata': Dictionary describing original count, duplicates removed, etc.
        """
        orig_count = len(chunks)

        # 1. Filter duplicates
        deduped = self.filter_duplicates(chunks)
        dedup_count = len(deduped)

        # 2. Filter topic
        topic_filtered = self.filter_topic(deduped, topic)
        topic_count = len(topic_filtered)

        # 3. Filter difficulty
        diff_filtered = self.filter_difficulty(topic_filtered, difficulty)
        diff_count = len(diff_filtered)

        # 4. Rank chunks
        ranked = self.rank_chunks(diff_filtered)

        # 5. Extract specific components (from the deduped pool, or from the fully filtered pool?
        # Let's extract from the deduped pool to make sure we don't miss follow-ups/deliverables
        # that might have slightly different topic/difficulty metadata but are relevant,
        # or from the fully filtered pool. Let's extract them from the deduped pool to be safe,
        # but filter them by topic if topic is set. Let's extract from diff_filtered).
        follow_ups = self.select_follow_ups(diff_filtered)
        deliverables = self.select_deliverables(diff_filtered)

        # Apply k limit to main ranked list if specified
        final_ranked = ranked[:k] if k is not None else ranked

        return {
            "ranked_chunks": final_ranked,
            "follow_ups": follow_ups,
            "deliverables": deliverables,
            "metadata": {
                "original_count": orig_count,
                "duplicates_removed": orig_count - dedup_count,
                "topic_mismatches_removed": dedup_count - topic_count,
                "difficulty_mismatches_removed": topic_count - diff_count,
                "final_count": len(final_ranked),
            },
        }

