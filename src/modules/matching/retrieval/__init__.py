"""Public evidence retrieval and lexical scoring API."""

from src.modules.matching.retrieval.bm25 import (
    bm25_section_maxsim,
    bm25_similarity,
    tokenize_text,
)
from src.modules.matching.retrieval.bm25_provider import (
    Bm25Provider,
    InMemoryBm25Provider,
    ParadeDbBm25Provider,
    get_bm25_provider,
)
from src.modules.matching.retrieval.evidence_retrieval import (
    EvidenceCandidate,
    is_named_technology,
    retrieve_named_technology_context,
    retrieve_semantic_evidence,
)
from src.modules.matching.retrieval.semantic import cosine_similarity

__all__ = [
    "Bm25Provider",
    "EvidenceCandidate",
    "InMemoryBm25Provider",
    "ParadeDbBm25Provider",
    "bm25_section_maxsim",
    "bm25_similarity",
    "cosine_similarity",
    "get_bm25_provider",
    "is_named_technology",
    "retrieve_named_technology_context",
    "retrieve_semantic_evidence",
    "tokenize_text",
]
