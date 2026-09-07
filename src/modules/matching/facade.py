from functools import lru_cache
from typing import Any

from src.modules.matching.rag.embedding import EmbeddingAdapter
from src.modules.matching.semantic import SemanticMatcher


class MatchingFacade:
    """Only supported entry point into matching from other business modules."""

    def __init__(self, embedder: EmbeddingAdapter | None = None) -> None:
        self.matcher = SemanticMatcher(embedder)

    def match(
        self,
        *,
        resume_text: str,
        job_description: str,
        algorithms: list[str],
        position: str | None = None,
        job_description_id: str | None = None,
        cv_id: str | None = None,
    ) -> dict[str, Any]:
        # ``algorithms`` remains in the API temporarily for old clients, but the
        # matching strategy is now always external embedding + cosine similarity.
        del algorithms
        return self.matcher.match(
            resume_text=resume_text,
            job_description=job_description,
            position=position,
            job_description_id=job_description_id,
            cv_id=cv_id,
        )


@lru_cache
def get_matching_facade() -> MatchingFacade:
    return MatchingFacade()
