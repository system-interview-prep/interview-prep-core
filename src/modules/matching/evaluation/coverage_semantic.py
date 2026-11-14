"""Score a pair by which needs are answered, not by how long the posting is.

The semantic factor joins every requirement label into one query and every CV
passage into one document, then scores the two texts against each other. BM25
sums over the query's terms, so a posting that asks for one more thing - even
something no candidate could evidence - scores differently for that reason
alone. Measured: adding an unmeetable requirement moved four pairs by +0.10 to
+0.78 points, and repeating one skill inside a CV moved another.

Neither is a flaw in BM25. It is a category error in how it was used: a
retrieval score ranks documents *for one query*, and this used it to compare
different queries against each other.

So the unit changes. Each requirement is scored against each passage, the
assignment between them is settled under a budget (see ``partial_transport``),
and the pair's score is the share of the posting's weighted needs that the
assignment answers. Adding a requirement nobody can meet then lowers the
score, because it enlarges what the posting asks for while answering none of
it - and that is the honest direction, not an artefact.

The hybrid stays hybrid: each requirement-passage pair is scored by the same
sparse and dense halves, in the same proportion, only at a smaller grain.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable

from src.modules.matching.retrieval.partial_transport import (
    MATERIAL_SHARE,
    TransportPlan,
    assign_evidence,
)

# A requirement contributes to the posting's total demand in proportion to how
# much the posting leans on it. These mirror the importance weights the
# requirement-coverage factor already uses.
IMPORTANCE = {"must_have": 1.0, "nice_to_have": 0.35, "context": 0.15}

# Pairs below this carry no signal worth transporting; keeping them would only
# let noise accumulate across a long posting.
MINIMUM_SIMILARITY = 0.30


@dataclass(frozen=True)
class ScoredRequirement:
    requirement_id: str
    priority: str
    text: str


@dataclass(frozen=True)
class ScoredSpan:
    evidence_id: str
    text: str


@dataclass(frozen=True)
class CoverageResult:
    score: float
    assignment: TransportPlan
    similarity: dict[tuple[str, str], float]
    duplicate_claims_removed: int
    answered_requirement_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]


def _fold(text: str) -> str:
    """Normalise a passage enough to recognise it when it is repeated."""
    decomposed = unicodedata.normalize("NFKD", (text or "").replace("đ", "d").replace("Đ", "D"))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip().casefold()


def deduplicate_spans(spans: list[ScoredSpan]) -> list[ScoredSpan]:
    """Two passages with the same words are one piece of evidence.

    Without this, copying a line inside a CV hands the assignment a second
    passage to spend, and a pair of requirements contending for one passage can
    suddenly have one each. The candidate would have gained nothing and the
    score would have risen anyway.
    """
    seen: set[str] = set()
    unique: list[ScoredSpan] = []
    for span in spans:
        key = _fold(span.text)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(span)
    return unique


def score_by_coverage(
    requirements: list[ScoredRequirement],
    spans: list[ScoredSpan],
    *,
    sparse_similarity: Callable[[str, str], float | None],
    dense_similarity: Callable[[str, str], float | None] | None = None,
    bm25_weight: float = 0.4,
    minimum_similarity: float = MINIMUM_SIMILARITY,
) -> CoverageResult | None:
    """The share of the posting's weighted needs the CV actually answers.

    Returns ``None`` when there is nothing to score, which the caller reports
    as an unscored factor rather than as a zero - a posting with no assessable
    requirement says nothing about the candidate.
    """
    scorable = [item for item in requirements if item.priority in IMPORTANCE and item.text.strip()]
    unique_spans = deduplicate_spans(spans)
    if not scorable or not unique_spans:
        return None

    similarity: dict[tuple[str, str], float] = {}
    for requirement in scorable:
        for span in unique_spans:
            sparse = sparse_similarity(requirement.text, span.text)
            dense = dense_similarity(requirement.text, span.text) if dense_similarity else None
            if sparse is None and dense is None:
                continue
            if dense is None:
                value = sparse or 0.0
            elif sparse is None:
                value = dense
            else:
                value = (1.0 - bm25_weight) * dense + bm25_weight * sparse
            if value >= minimum_similarity:
                similarity[(requirement.requirement_id, span.evidence_id)] = min(1.0, value)

    masses = {item.requirement_id: IMPORTANCE[item.priority] for item in scorable}
    assignment = assign_evidence(
        [item.requirement_id for item in scorable],
        [span.evidence_id for span in unique_spans],
        similarity,
        requirement_mass=masses,
    )

    demand = sum(masses.values())
    answered = sum(assignment.mass_for(item.requirement_id) for item in scorable)
    score = min(1.0, max(0.0, answered / demand)) if demand else 0.0

    answered_ids = tuple(
        item.requirement_id
        for item in scorable
        if assignment.mass_for(item.requirement_id) >= MATERIAL_SHARE * masses[item.requirement_id]
    )
    held = {
        span.evidence_id
        for span in unique_spans
        if assignment.holders_of(span.evidence_id)
    }
    claimed = sum(1 for value in similarity.values() if value >= minimum_similarity)
    kept = sum(
        1
        for row in assignment.plan
        for value in row
        if value > MATERIAL_SHARE
    )
    return CoverageResult(
        score=round(score, 6),
        assignment=assignment,
        similarity=similarity,
        duplicate_claims_removed=max(0, claimed - kept),
        answered_requirement_ids=answered_ids,
        evidence_refs=tuple(sorted(held)),
    )
