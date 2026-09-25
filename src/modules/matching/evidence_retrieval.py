"""Requirement-level evidence retrieval.

Retrieval is deliberately separated from the status decision.  This module may
surface a semantically related evidence span, but the evaluator still decides
whether its strength and context are sufficient for ``met``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from src.modules.matching.bm25 import bm25_similarity
from src.modules.user_cvs.schemas import CanonicalResume, EvidenceSpan


@dataclass(frozen=True)
class EvidenceCandidate:
    evidence: EvidenceSpan
    retrieval_method: str
    semantic_score: float
    lexical_score: float
    dense_score: float | None = None
    concept_id: str | None = None

    def trace(self, rank: int, evidence_strength: str) -> dict[str, object]:
        return {
            "evidence_ref": self.evidence.evidence_id,
            "concept_id": self.concept_id,
            "retrieval_method": self.retrieval_method,
            "lexical_score": self.lexical_score,
            "dense_score": self.dense_score,
            "semantic_score": self.semantic_score,
            "score_model": "capability-expansion-bm25-v1",
            "threshold_version": "semantic-expansion-v1",
            "evidence_strength": evidence_strength,
            "rank": rank,
        }


# These are capability paraphrases, not aliases for named products.  A generic
# phrase such as "message broker" must never be mapped to Kafka by this table.
_CAPABILITY_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "high performance": (
        "response time",
        "p95",
        "p99",
        "requests per second",
        "load test",
        "response-time tuning",
        "capacity",
    ),
    "high throughput": (
        "requests per second",
        "request volume",
        "records per day",
        "large request volumes",
        "high volume",
        "throughput",
    ),
    "low latency": ("response time", "p95", "p99", "latency", "sub-millisecond"),
    "rest apis": ("resource-oriented http", "http endpoints", "http interface", "resource endpoint"),
    "microservices": ("service-oriented", "modular services", "independent services"),
    "distributed systems": ("multi-node", "cluster", "distributed", "node"),
    "multithreading": ("parallel worker", "worker pool", "thread pool", "parallel execution"),
    "concurrency": ("parallel", "race-safe", "simultaneous", "worker pool", "back-pressure"),
    "asynchronous programming": ("non-blocking i/o", "task-based", "async", "event loop"),
    "database design": (
        "relational schema",
        "modelled relational",
        "schema modelling",
        "data model",
        "table design",
    ),
    "indexing": ("access path", "access paths", "index", "data layout"),
    "query optimization": (
        "query plan",
        "query-plan",
        "query plan analysis",
        "query performance",
        "reduced report",
    ),
    "transactions": ("atomic commit", "atomic state", "idempotent command", "consistent state"),
    "debugging": ("root-cause analysis", "incident", "failure injection", "production issue"),
    "problem solving": ("root-cause analysis", "remediation", "troubleshooting", "failure recovery"),
    "performance optimization": ("response-time tuning", "load test", "benchmark", "flame graph"),
    "real time": ("continuously flowing", "event handler", "streaming", "live processing"),
    "transaction processing": ("settlement", "ledger", "atomic state", "order workflow"),
    "large scale data": ("records per day", "large volume", "million records", "high-volume"),
    "high availability": ("fault recovery", "resilient", "failure recovery", "redundancy"),
    "fault tolerance": ("retry policy", "failure recovery", "load shedding", "resilient"),
    "monitoring": ("telemetry", "alerting", "observability", "metrics"),
    "ci cd": ("automated release", "delivery pipeline", "continuous delivery", "release pipeline"),
    # Domain capabilities used by finance/transaction requirements.  These are
    # intentionally broad capability terms, not aliases for a named product.
    "finance": ("payment", "settlement", "reconciliation", "ledger", "authorization"),
    "financial systems": ("payment", "settlement", "reconciliation", "ledger", "authorization"),
    "fintech": ("payment", "settlement", "payment authorization", "ledger", "order workflow"),
    "trading platforms": ("order routing", "order workflow", "settlement", "execution"),
    "high volume transaction systems": (
        "settlement",
        "ledger",
        "transaction processing",
        "records per day",
        "million records",
    ),
}

_NAMED_TECHNOLOGIES = {
    "c#",
    ".net",
    "kafka",
    "redis",
    "mysql",
    "aws",
    "kubernetes",
    "docker",
    "linux",
    "python",
    "java",
    "postgresql",
}

# Context phrases can support a named technology claim without silently
# upgrading it to ``met``.  They are used only to attach explainable evidence
# to an ``unknown`` result when the CV describes the surrounding environment
# (e.g. OCI/container packaging and Unix-like hosts) but omits the product
# name itself.
_NAMED_TECH_CONTEXT: dict[str, tuple[str, ...]] = {
    "docker": ("oci image", "container image", "container packaging", "containerized"),
    "linux": ("unix-like", "unix host", "unix-like operations", "posix"),
}


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9+#.]+", ascii_value.casefold()))


def _contains_phrase(text: str, phrase: str) -> bool:
    text_tokens = _normalize(text).replace('.', ' ').split()
    phrase_tokens = _normalize(phrase).replace('.', ' ').split()
    if not phrase_tokens or len(phrase_tokens) > len(text_tokens):
        return False
    for start in range(len(text_tokens) - len(phrase_tokens) + 1):
        window = text_tokens[start : start + len(phrase_tokens)]
        if all(
            actual == expected
            or (actual == f"{expected}s")
            or (expected == f"{actual}s")
            for actual, expected in zip(window, phrase_tokens, strict=True)
        ):
            return True
    return False


def _query_expansions(query: str) -> list[str]:
    normalized = _normalize(query).replace(".", " ")
    expansions: list[str] = []
    for trigger, values in _CAPABILITY_EXPANSIONS.items():
        if f" {_normalize(trigger).replace('.', ' ')} " in f" {normalized} ":
            expansions.extend(values)
    return list(dict.fromkeys(expansions))


def is_named_technology(label: str) -> bool:
    normalized = _normalize(label).replace(".", " ")
    return any(
        f" {_normalize(value).replace('.', ' ')} " in f" {normalized} "
        for value in _NAMED_TECHNOLOGIES
    )


def retrieve_semantic_evidence(
    query: str,
    resume: CanonicalResume,
    *,
    concept_id: str | None = None,
    allow_named_technology: bool = False,
    minimum_score: float = 0.30,
) -> list[EvidenceCandidate]:
    """Return ranked capability candidates without deciding requirement status.

    Dense retrieval is intentionally optional at this boundary.  The deterministic
    expansion layer provides a reproducible baseline and can later be combined
    with dense scores without changing the evaluator contract.
    """
    if is_named_technology(query) and not allow_named_technology:
        return []
    expansions = _query_expansions(query)
    if not expansions:
        return []

    candidates: list[EvidenceCandidate] = []
    for evidence in resume.evidence:
        matched = [phrase for phrase in expansions if _contains_phrase(evidence.text, phrase)]
        if not matched:
            continue
        semantic_score = min(1.0, len(matched) / max(2.0, min(5.0, len(expansions))))
        lexical_score = bm25_similarity(" ".join(matched), evidence.text, reference_length=40.0)
        combined = round(0.7 * semantic_score + 0.3 * lexical_score, 4)
        if combined < minimum_score:
            continue
        candidates.append(
            EvidenceCandidate(
                evidence=evidence,
                retrieval_method="semantic_lexical_expansion",
                semantic_score=combined,
                lexical_score=lexical_score,
                concept_id=concept_id,
            )
        )
    return sorted(
        candidates,
        key=lambda item: (-item.semantic_score, item.evidence.char_start, item.evidence.evidence_id),
    )[:4]


def retrieve_named_technology_context(
    concept_label: str,
    requirement_text: str,
    resume: CanonicalResume,
) -> list[EvidenceCandidate]:
    """Retrieve contextual evidence without asserting a named technology.

    This intentionally returns candidates for presentation/verification only;
    callers must keep the status ``unknown`` until the exact technology is
    confirmed by a structured claim or explicit alias.
    """

    label = _normalize(concept_label).replace(".", " ")
    context = _NAMED_TECH_CONTEXT.get(label)
    if not context or not _contains_phrase(requirement_text, concept_label):
        return []
    candidates: list[EvidenceCandidate] = []
    for evidence in resume.evidence:
        matched = [phrase for phrase in context if _contains_phrase(evidence.text, phrase)]
        if not matched:
            continue
        semantic_score = min(0.58, 0.35 + 0.08 * len(matched))
        lexical_score = bm25_similarity(" ".join(matched), evidence.text, reference_length=40.0)
        candidates.append(
            EvidenceCandidate(
                evidence=evidence,
                retrieval_method="named_technology_context",
                semantic_score=round(0.7 * semantic_score + 0.3 * lexical_score, 4),
                lexical_score=lexical_score,
                concept_id=None,
            )
        )
    return sorted(
        candidates,
        key=lambda item: (-item.semantic_score, item.evidence.char_start, item.evidence.evidence_id),
    )[:4]
