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


def content_tokens(text: str) -> list[str]:
    """The syllables a requirement is actually about, folded for comparison."""
    return [token for token in _fold(text).split() if len(token) > 1]


def shares_enough_wording(requirement_text: str, span_text: str) -> bool:
    """Whether the span repeats enough of the requirement to be about it.

    A similarity floor alone is not a guard in Vietnamese. Whitespace yields
    syllables rather than words, and folding the diacritics makes them collide,
    so a long requirement built from common syllables can clear any floor
    against text that has nothing to do with it: measured here, the probe
    "kinh nghiem van hanh lo phan ung hat nhan" drew real mass from an ordinary
    CV and raised the pair's score.

    The requirement evaluator already answers this with a strict majority of
    the requirement's own syllables, scaling the bar with the size of the
    claim. The same rule applies here, for the same reason.
    """
    wanted = content_tokens(requirement_text)
    if not wanted:
        return False
    present = set(content_tokens(span_text))
    needed = max(2, len(wanted) // 2 + 1) if len(wanted) > 1 else 1
    return sum(1 for token in wanted if token in present) >= needed


def credit_for(similarity_value: float, floor: float) -> float:
    """What a requirement earns for its best evidence, measured from the floor.

    ``floor`` is a noise gate: below it a pair carries no signal worth
    transporting. Reading the gate's threshold back as a credit conflates two
    questions - whether to count a passage at all, and what it is worth - and
    makes the score jump. Evidence at 0.299 earned nothing and evidence at
    0.301 earned 0.301, a step of nearly a third of the scale for a difference
    no reader could see. On a posting with three requirements that step is
    about ten points of the published profile match, enough to put two
    near-identical CVs in different bands.

    It also left a residue of the very defect this module exists to remove.
    The score is a weighted mean, so adding a requirement raises it whenever
    that requirement outscores the mean - and below the floor *everything*
    that clears the gate does. Measured under MR2 on the frozen corpus, three
    of the four surviving violations were pairs scoring 0.109, 0.233 and
    0.261, each lifted by a probe nobody could meet that landed just above the
    gate. The fourth, scoring 0.281, is the control: there the probe cleared
    nothing, and the score fell as it should.

    Credit therefore grows from the floor: evidence at the noise floor is
    worth nothing, which is what calling it a noise floor means.
    """
    if similarity_value < floor:
        return 0.0
    if floor >= 1.0:
        return 1.0
    return (similarity_value - floor) / (1.0 - floor)


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
    # Mean of each half over the pairs that cleared the floor. The score itself
    # has no single document-level sparse or dense value any more, but a reader
    # of the trace still needs to see which half carried it.
    mean_sparse: float | None
    mean_dense: float | None
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
    sparse_parts: list[float] = []
    dense_parts: list[float] = []
    for requirement in scorable:
        for span in unique_spans:
            # The guard belongs to the sparse half alone. Syllable collision is
            # a property of matching on shared characters; an embedding does
            # not confuse a nuclear reactor with a degree because they spell
            # alike. Suppressing the lexical half here rather than dropping the
            # pair leaves the dense half free to do the job it is there for -
            # recognising the same thing said in other words.
            lexical_support = shares_enough_wording(requirement.text, span.text)
            sparse = sparse_similarity(requirement.text, span.text) if lexical_support else 0.0
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
                if sparse is not None:
                    sparse_parts.append(sparse)
                if dense is not None:
                    dense_parts.append(dense)

    masses = {item.requirement_id: IMPORTANCE[item.priority] for item in scorable}
    assignment = assign_evidence(
        [item.requirement_id for item in scorable],
        [span.evidence_id for span in unique_spans],
        similarity,
        requirement_mass=masses,
    )

    # A requirement's credit is its own best evidence, not its share of the
    # transported mass. Mass is a shared resource, so reading credit off it
    # made one requirement's score depend on how many others were in the
    # posting: measured under MR2, adding a single unmeetable requirement
    # moved the pair by +14, +16 and once +54 points, where the aggregation it
    # replaced moved by less than one. The budget still decides *who* may
    # count a contested passage - that is what it is for - but once it has
    # decided, what the holder scores is a property of that pair alone.
    best_evidence: dict[str, float] = {}
    credits: dict[str, float] = {}
    for item in scorable:
        held = [
            similarity[(item.requirement_id, span.evidence_id)]
            for span in unique_spans
            if (item.requirement_id, span.evidence_id) in similarity
            and item.requirement_id in assignment.holders_of(span.evidence_id)
        ]
        best_evidence[item.requirement_id] = max(held) if held else 0.0
        credits[item.requirement_id] = credit_for(
            best_evidence[item.requirement_id], minimum_similarity
        )

    demand = sum(masses.values())
    answered = sum(masses[key] * value for key, value in credits.items())
    score = min(1.0, max(0.0, answered / demand)) if demand else 0.0

    # Reported on the evidence itself, not on the rescaled credit: a
    # requirement whose passage cleared the gate *is* answered, even where the
    # credit it earns above the gate rounds to little.
    answered_ids = tuple(
        item.requirement_id
        for item in scorable
        if best_evidence[item.requirement_id] >= minimum_similarity
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
        mean_sparse=round(sum(sparse_parts) / len(sparse_parts), 6) if sparse_parts else None,
        mean_dense=round(sum(dense_parts) / len(dense_parts), 6) if dense_parts else None,
        assignment=assignment,
        similarity=similarity,
        duplicate_claims_removed=max(0, claimed - kept),
        answered_requirement_ids=answered_ids,
        evidence_refs=tuple(sorted(held)),
    )
