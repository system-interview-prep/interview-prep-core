"""Reconciling LLM requirement claims with a block-wide deterministic row.

A scraped posting often stores its whole requirements block as one run-on line
with no separators between items. The deterministic parser is line-bounded, so
it yields a single requirement whose evidence span covers the entire block. The
reconciliation in ``_merge`` used to test bare span intersection, so every
distinct LLM claim inside that block merged into that one row - each merge
overwriting the previous one's label and concept. Measured across ten real
postings: 116 extracted requirements collapsed into 26 rows, one of them holding
ten evidence refs, an education id, a CSS concept and the text of the last claim.

A baseline row that contains several much smaller claims is a container for
them, not a peer of any one of them.
"""

from __future__ import annotations

import hashlib

import pytest

from src.modules.job_descriptions.parsing.hybrid import HybridJobDescriptionParser
from src.modules.job_descriptions.parsing.llm_candidate import (
    JobDescriptionCandidate,
    RequirementCandidate,
)
from src.modules.user_cvs.parsing.domain.source import SourceBlock, SourceDocument

_HEAD = "Job title: Junior Developer\n\nJob requirements:\n"

# One run-on requirements line. The deterministic pass anchors a single
# education requirement whose span covers the whole block.
CONTAINER_TEXT = _HEAD + (
    "Degree: Bachelor of Information Technology.Knowledge of relational databases."
    "Able to read English documents.Eager to learn.Strong logical thinking."
)

# The same shape, but the block-wide row also carries a parsed duration that no
# sentence-level claim restates.
CONTAINER_WITH_DURATION_TEXT = _HEAD + (
    "Degree: Bachelor of IT with minimum 3 years experience.Knowledge of relational databases."
    "Able to read English.Eager to learn."
)

# Here the deterministic pass anchors a narrow taxonomy alias instead, so there
# is no container and the ordinary 1:1 reconciliation applies.
NARROW_BASELINE_TEXT = (
    "Job title: Junior Sql Developer\n\nJob requirements:\n"
    "Tot nghiep chuyen nganh CNTT.Co kien thuc ve SQL.Co kha nang doc hieu tieng Anh."
)

CLAIMS = [
    ("Bachelor of Information Technology", "education", "must_have"),
    ("Knowledge of relational databases", "skill", "must_have"),
    ("Able to read English documents", "language", "must_have"),
    ("Eager to learn", "other", "preferred"),
    ("Strong logical thinking", "other", "preferred"),
]


def _source(text: str) -> SourceDocument:
    blocks, position = [], 0
    for index, line in enumerate(text.splitlines(keepends=True)):
        stripped = line.rstrip("\n")
        if stripped.strip():
            blocks.append(
                SourceBlock(
                    block_id=f"b{index}",
                    text=stripped,
                    page=1,
                    reading_order=index,
                    bounding_box=None,
                    block_type="paragraph",
                    char_start=position,
                    char_end=position + len(stripped),
                    section="job_description",
                )
            )
        position += len(line)
    return SourceDocument(
        document_id="probe",
        document_sha256=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
        blocks=tuple(blocks),
    )


def _merge(text: str, quotes: list[tuple[str, str, str]]):
    parser = HybridJobDescriptionParser()
    source = _source(text)
    baseline = parser._deterministic.parse(source, extraction_version="probe")
    for quote, _, _ in quotes:
        assert quote in text, f"quote must appear verbatim in the source: {quote!r}"
    candidate = JobDescriptionCandidate(
        requirements=[
            RequirementCandidate(value=quote, quote=quote, kind=kind, priority=priority)
            for quote, kind, priority in quotes
        ]
    )
    return baseline, parser._merge(baseline, source, candidate)


def test_a_block_wide_baseline_row_does_not_swallow_the_claims_inside_it() -> None:
    baseline, merged = _merge(CONTAINER_TEXT, CLAIMS)

    # The premise: the deterministic pass produced one block-wide row.
    assert len(baseline.requirements) == 1

    # Every claim survives as its own requirement rather than collapsing into it.
    assert {item.raw_label for item in merged.requirements} == {quote for quote, _, _ in CLAIMS}

    # Carrying no structured detail, the container itself is superseded.
    assert baseline.requirements[0].requirement_id not in {
        item.requirement_id for item in merged.requirements
    }

    # No row hoards the whole block's evidence.
    assert max(len(item.evidence_refs) for item in merged.requirements) == 1


def test_each_promoted_claim_keeps_its_own_kind_and_priority() -> None:
    """The collapsed row reported a single kind and label for the whole block."""
    _, merged = _merge(CONTAINER_TEXT, CLAIMS)
    by_label = {item.raw_label: item for item in merged.requirements}

    assert by_label["Able to read English documents"].kind == "language"
    assert by_label["Bachelor of Information Technology"].kind == "education"
    assert by_label["Eager to learn"].priority == "preferred"
    assert by_label["Knowledge of relational databases"].priority == "must_have"


def test_a_container_holding_a_duration_is_kept_but_can_no_longer_disqualify() -> None:
    """The parsed duration is detail no sentence-level claim restates.

    Keeping the row preserves it; demoting to ``preferred`` - which maps to
    ``nice_to_have`` at the matching boundary - stops it gating eligibility on a
    claim that is really the whole block.
    """
    quotes = [
        ("Bachelor of IT", "education", "must_have"),
        ("Knowledge of relational databases", "skill", "must_have"),
        ("Able to read English", "language", "must_have"),
        ("Eager to learn", "other", "preferred"),
    ]
    baseline, merged = _merge(CONTAINER_WITH_DURATION_TEXT, quotes)

    assert len(baseline.requirements) == 1
    container_id = baseline.requirements[0].requirement_id
    assert baseline.requirements[0].minimum_experience_months == 36
    assert baseline.requirements[0].priority == "must_have"

    kept = {item.requirement_id: item for item in merged.requirements}
    assert container_id in kept, "a container carrying a duration must not be dropped"
    assert kept[container_id].minimum_experience_months == 36
    assert kept[container_id].priority == "preferred"

    # Its members are still promoted alongside it.
    assert {quote for quote, _, _ in quotes} <= {item.raw_label for item in merged.requirements}


def test_a_single_claim_inside_a_block_is_not_a_container() -> None:
    """One claim cannot tell a block apart from a requirement: merge as before."""
    quotes = [("Knowledge of relational databases", "skill", "must_have")]
    baseline, merged = _merge(CONTAINER_TEXT, quotes)

    assert len(baseline.requirements) == 1
    assert len(merged.requirements) == 1
    merged_row = merged.requirements[0]
    assert merged_row.requirement_id == baseline.requirements[0].requirement_id
    assert merged_row.raw_label == "Knowledge of relational databases"
    assert len(merged_row.evidence_refs) == 2


def test_merging_into_a_narrow_baseline_row_is_unchanged() -> None:
    """Where the baseline is a real requirement, 1:1 reconciliation still applies."""
    quotes = [("Co kien thuc ve SQL", "skill", "must_have")]
    baseline, merged = _merge(NARROW_BASELINE_TEXT, quotes)

    assert len(baseline.requirements) == 1
    assert len(merged.requirements) == 1
    merged_row = merged.requirements[0]
    assert merged_row.requirement_id == baseline.requirements[0].requirement_id
    assert merged_row.raw_label == "Co kien thuc ve SQL"
    assert len(merged_row.evidence_refs) == 2


@pytest.mark.parametrize("claim_count", [2, 3, 5])
def test_two_contained_claims_are_enough_to_treat_a_row_as_a_container(
    claim_count: int,
) -> None:
    _, merged = _merge(CONTAINER_TEXT, CLAIMS[:claim_count])

    assert {item.raw_label for item in merged.requirements} == {
        quote for quote, _, _ in CLAIMS[:claim_count]
    }
    assert max(len(item.evidence_refs) for item in merged.requirements) == 1


def test_promoted_claims_resolve_their_own_concepts() -> None:
    """The collapsed row carried one concept for the whole block - the wrong one."""
    quotes = [
        ("Co kien thuc ve SQL", "skill", "must_have"),
        ("Co kha nang doc hieu tieng Anh", "language", "must_have"),
        ("Tot nghiep chuyen nganh CNTT", "education", "must_have"),
    ]
    _, merged = _merge(NARROW_BASELINE_TEXT, quotes)
    by_label = {item.raw_label: item for item in merged.requirements}

    sql = by_label["Co kien thuc ve SQL"]
    assert sql.concept is not None
    assert "sql" in sql.concept.concept_id.casefold()
    # A language claim must not inherit the SQL concept.
    assert by_label["Co kha nang doc hieu tieng Anh"].concept is None
