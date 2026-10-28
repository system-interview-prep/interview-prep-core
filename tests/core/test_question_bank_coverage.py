"""Dev-gate: the seeded Question Bank must cover every concept real JDs produce.

Question selection is fail-closed on both branches, so a single JD requirement
concept with no approved question refuses the whole interview session with
`question_bank_insufficient`. That failure only shows up at runtime, on a real
candidate, which is the worst place to discover it.

These tests turn that into a build-time failure by diffing the JD golden corpus
against the seed fixtures. They are skipped when the private DOC_AND_PLAN corpus
is not checked out alongside the backend (as in CI).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from src.modules.interviews.planning.planner import _MAX_QUESTIONS_PER_TARGET
from src.modules.taxonomy.career import _RULES
from src.seeds.question_bank_seed import _CONCEPT_FIXTURES

_JD_GOLDEN_ROOT = Path(__file__).resolve().parents[2].parent / "DOC_AND_PLAN" / "data" / "eval" / "jd"


def _seeded_question_counts() -> dict[str, int]:
    return {fixture["concept_id"]: len(fixture["questions"]) for fixture in _CONCEPT_FIXTURES}


def _role_question_counts() -> Counter[str]:
    counts: Counter[str] = Counter()
    for fixture in _CONCEPT_FIXTURES:
        for role_code in fixture.get("role_concepts", ()):
            counts[role_code] += len(fixture["questions"])
    return counts


def _jd_golden_concept_ids() -> Counter[str]:
    """Every taxonomy concept id referenced anywhere in the JD golden corpus."""
    found: Counter[str] = Counter()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"concept_id", "conceptId"} and isinstance(value, str):
                    found[value] += 1
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for path in _JD_GOLDEN_ROOT.rglob("*.json"):
        try:
            walk(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            # Reviewer exports and checksum siblings are not all parseable JD data.
            continue
    return found


_corpus_missing = pytest.mark.skipif(
    not _JD_GOLDEN_ROOT.is_dir(),
    reason="DOC_AND_PLAN JD golden corpus is not checked out next to the backend",
)


@_corpus_missing
def test_every_jd_golden_concept_has_seeded_questions() -> None:
    """A concept a real JD asks for, with no approved question, refuses the session."""
    seeded = _seeded_question_counts()
    referenced = _jd_golden_concept_ids()
    assert referenced, "JD golden corpus produced no concept ids; the scan is broken"

    uncovered = sorted(
        ((concept, count) for concept, count in referenced.items() if concept not in seeded),
        key=lambda item: -item[1],
    )
    assert not uncovered, (
        "JD concepts with no seeded question (each one fails interview selection closed): "
        + ", ".join(f"{concept} (seen {count}x)" for concept, count in uncovered)
    )


@_corpus_missing
def test_seeded_concepts_can_satisfy_a_single_target_plan() -> None:
    """One target can be allocated up to `_MAX_QUESTIONS_PER_TARGET` questions.

    A concept seeded with fewer than that still fails closed whenever the planner
    concentrates the budget on it, so the floor is the cap, not one.
    """
    seeded = _seeded_question_counts()
    referenced = _jd_golden_concept_ids()

    thin = sorted(
        (concept, seeded[concept])
        for concept in referenced
        if concept in seeded and seeded[concept] < _MAX_QUESTIONS_PER_TARGET
    )
    assert not thin, (
        f"Concepts seeded with fewer than {_MAX_QUESTIONS_PER_TARGET} questions: {thin}"
    )


def test_every_career_code_has_target_role_questions() -> None:
    """P1's career-classification fallback accepts only TARGET_ROLE mappings.

    A career code with no TARGET_ROLE question makes that fallback fail closed at
    P2 every time, which is how the fallback silently never worked.
    """
    role_counts = _role_question_counts()
    missing = sorted(rule.code for rule in _RULES if role_counts[rule.code] == 0)
    assert not missing, f"Career codes with no TARGET_ROLE question: {missing}"
