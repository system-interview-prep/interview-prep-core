"""Regression over real job postings captured from public Greenhouse boards.

Rule-based parsing kept breaking on headings and layouts that hand-written
fixtures never had ("What We Look For In You", company boilerplate after the
requirements, "Bonus points" bullets). Every rule change is measured against
these postings instead of one case at a time. Expectations are floors: a better
parser may find more requirements, never fewer skills or a different role.
"""

import json
import re
from pathlib import Path

import pytest

from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser
from src.modules.user_cvs.facade import SourceBlock, SourceDocument

_CASES = (
    Path(__file__).resolve().parents[3].parent
    / "DOC_AND_PLAN"
    / "data"
    / "eval"
    / "jd"
    / "greenhouse_live_v1"
    / "cases.json"
)
_NOISE = re.compile(r"https?://|^#|^about\b|^other things|^equal (?:opportunity|employment)", re.I)


def _cases() -> list[dict]:
    if not _CASES.exists():
        return []
    return json.loads(_CASES.read_text(encoding="utf-8"))["cases"]


def _source(case: dict) -> SourceDocument:
    text = case["raw_text"]
    block = SourceBlock(
        block_id="block-0000",
        text=text,
        page=1,
        reading_order=0,
        bounding_box=None,
        block_type="text",
        char_start=0,
        char_end=len(text),
        section="other",
    )
    return SourceDocument(document_id=case["case_id"], document_sha256="0" * 64, text=text, blocks=(block,))


@pytest.mark.skipif(not _cases(), reason="DOC_AND_PLAN greenhouse_live_v1 golden is not checked out")
@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["case_id"])
def test_live_posting_parses_to_its_reviewed_floor(case: dict) -> None:
    parsed = DeterministicJobDescriptionParser().parse(_source(case), extraction_version="golden")
    expected = case["expected"]

    assert len(parsed.requirements) >= expected["min_requirements"]
    found = {
        ref.concept_id
        for requirement in parsed.requirements
        for ref in ([requirement.concept] if requirement.concept else requirement.atomic_concepts)
    }
    assert set(expected["required_concepts"]) <= found
    noise = [r.raw_label for r in parsed.requirements if _NOISE.search(r.raw_label.strip())]
    assert not noise, f"boilerplate parsed as requirements: {noise}"

    # Board ingestion classifies with the posting title it trusts.
    classifications = DeterministicJobDescriptionParser._classifications(
        parsed.requirements, trusted_title=case["title"]
    )
    assert classifications[0].code == expected["primary_classification"]
