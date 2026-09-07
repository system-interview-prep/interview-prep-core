import pytest

from src.modules.matching.facade import MatchingFacade


class StubEmbedder:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        assert len(texts) == 2
        return [[1.0, 0.0, 1.0], [1.0, 0.0, 1.0]]


def test_external_embedding_cosine_pipeline() -> None:
    result = MatchingFacade(StubEmbedder()).match(
        resume_text="Python FastAPI PostgreSQL REST API",
        job_description="Requires Python, PostgreSQL and REST API experience",
        algorithms=["embedding_cosine"],
    )

    assert result["metadata"]["algorithms_used"] == ["embedding_cosine"]
    assert list(result["individual_scores"]) == ["embedding_cosine"]
    assert len(result["combined_results"]) == 1
    assert result["combined_results"][0]["combined_score"] == pytest.approx(1.0)
