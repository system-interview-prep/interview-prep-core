import math
import os
from typing import Any

from src.modules.matching.rag.embedding import EmbeddingAdapter, build_embedding_adapter_from_env


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        raise ValueError("Embedding provider returned an empty vector")
    if len(left) != len(right):
        raise ValueError("CV and JD embedding dimensions do not match")

    dot_product = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        raise ValueError("Embedding provider returned a zero vector")

    return max(0.0, min(1.0, dot_product / (left_norm * right_norm)))


class SemanticMatcher:
    def __init__(self, embedder: EmbeddingAdapter | None = None) -> None:
        self.embedder = embedder or build_embedding_adapter_from_env()

    def match(
        self,
        *,
        resume_text: str,
        job_description: str,
        position: str | None = None,
        job_id: str | None = None,
        cv_id: str | None = None,
    ) -> dict[str, Any]:
        vectors = self.embedder.embed_texts([resume_text, job_description])
        if len(vectors) != 2:
            raise RuntimeError("Embedding provider must return one vector per input")

        score = cosine_similarity(vectors[0], vectors[1])
        provider = os.getenv("EMBEDDING_PROVIDER", "").strip().lower()
        model = os.getenv("EMBEDDING_MODEL", "").strip()
        return {
            "metadata": {
                "algorithms_used": ["embedding_cosine"],
                "embedding_provider": provider,
                "embedding_model": model,
                "embedding_dimension": len(vectors[0]),
                "job_position": position,
                "job_id": job_id,
                "cv_id": cv_id,
            },
            "individual_scores": {
                "embedding_cosine": [{"score": score, "resume_index": 0}],
            },
            "combined_results": [
                {
                    "resume_index": 0,
                    "combined_score": score,
                    "weighted_score": score,
                    "rank": 1,
                }
            ],
        }
