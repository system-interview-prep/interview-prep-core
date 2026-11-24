"""Checklist grading: the grader labels each expected point, code computes the score.

Asking an LLM for a 0-10 number gave different scores for the same answer
(the model in use does not accept a temperature). Here the grader only says,
for each expected point of the question, whether the candidate covered it and
quotes where. Code then:

- keeps a label only when its quote is found verbatim in the answer, so a
  point cannot be credited (or penalised) on invented text;
- computes the score with a fixed formula from the verified labels.

What the quote check cannot prove: that a real quote supports the point it is
attached to. Two cheap guards reduce that risk (a negation the point does not
contain, and one quote credited to several core points); both only downgrade.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

COVERED = "COVERED"
PARTIAL = "PARTIAL"
INCORRECT = "INCORRECT"
MISSING = "MISSING"
STATUSES = (COVERED, PARTIAL, INCORRECT, MISSING)

CORE = "CORE"
BONUS = "BONUS"

# Score anchors (kept from the holistic rubric): every core point covered is
# 9.0; bonus points lift the score within 9-10. Without bonus points, full core
# coverage is 10.
CORE_MAX_WITH_BONUS = 9.0
CREDIT = {COVERED: 1.0, PARTIAL: 0.5, INCORRECT: 0.0, MISSING: 0.0}
# Stating something wrong is worse than leaving it out.
INCORRECT_PENALTY = 1.0
# A quote must carry at least this many words to count as evidence.
MIN_QUOTE_WORDS = 2

_FOLLOW_UP_MARKER = re.compile(r"\[follow-up answer\]\s*:", re.IGNORECASE)
# No bare English "no": it is part of names like "no-floating-promises", "NoSQL".
_NEGATION = re.compile(r"(?<!\w)(không|chẳng|chưa|ko|đừng|not|never|don't|dont|doesn't)(?!\w)")


@dataclass(frozen=True)
class ExpectedPoint:
    key: str
    description: str
    importance: str  # CORE | BONUS


def expected_points_from_snapshot(points: Any) -> list[ExpectedPoint]:
    """Expected points of a question snapshot, in display order; empty when it has none."""
    if not isinstance(points, list):
        return []
    parsed: list[tuple[int, ExpectedPoint]] = []
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            continue
        description = str(point.get("description") or "").strip()
        if not description:
            continue
        key = str(point.get("stable_key") or point.get("stableKey") or f"point-{index}")
        importance = BONUS if str(point.get("importance") or "").upper() == BONUS else CORE
        order = point.get("display_order", point.get("displayOrder", index))
        position = order if isinstance(order, int) else index
        parsed.append((position, ExpectedPoint(key, description, importance)))
    parsed.sort(key=lambda item: item[0])
    keys = [point.key for _, point in parsed]
    if len(set(keys)) != len(keys):
        # Keys must identify points; fall back to positions rather than merge two.
        return [ExpectedPoint(f"point-{i}", p.description, p.importance) for i, (_, p) in enumerate(parsed)]
    return [point for _, point in parsed]


def normalize_for_match(text: str) -> str:
    """Lowercase, NFC, punctuation and whitespace collapsed. Diacritics are kept:
    without them "bán" and "bàn" would match."""
    text = _FOLLOW_UP_MARKER.sub(" ", unicodedata.normalize("NFC", text).lower())
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def quote_is_verified(quote: str, answer: str) -> bool:
    """True when the quote appears in the answer (after normalisation) and is not trivial."""
    needle = normalize_for_match(quote)
    if len(needle.split()) < MIN_QUOTE_WORDS:
        return False
    return f" {needle} " in f" {normalize_for_match(answer)} "


def verified_quotes(quotes: Any, answer: str) -> list[str]:
    """Quotes from a grader that really occur in the answer; the rest are dropped."""
    if not isinstance(quotes, list):
        return []
    return [str(quote) for quote in quotes if isinstance(quote, str) and quote_is_verified(quote, answer)]


@dataclass
class PointVerdict:
    key: str
    description: str
    importance: str
    status: str
    quote: str = ""
    # Why the grader's label was changed: unverified_quote, no_quote,
    # negation_in_quote, quote_reused.
    adjusted: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "description": self.description,
            "importance": self.importance,
            "status": self.status,
            "quote": self.quote,
            "adjusted": self.adjusted,
        }


class ChecklistOutputError(ValueError):
    """The grader's checklist cannot be used; the turn is retried."""


def validate_checklist(
    raw_points: Any, expected: list[ExpectedPoint], answer: str
) -> list[PointVerdict]:
    """Turn the grader's per-point labels into verified verdicts, one per expected point."""
    if not isinstance(raw_points, list):
        raise ChecklistOutputError("Grader returned no point list")
    by_key: dict[str, dict] = {}
    for item in raw_points:
        if isinstance(item, dict):
            key = str(item.get("stable_key") or item.get("key") or "")
            by_key.setdefault(key, item)  # duplicates: the first label wins
    labelled = sum(1 for point in expected if point.key in by_key)
    if labelled * 2 < len(expected):
        raise ChecklistOutputError(f"Grader labelled only {labelled}/{len(expected)} points")

    verdicts: list[PointVerdict] = []
    credited_core_quotes: set[str] = set()
    for point in expected:
        item = by_key.get(point.key) or {}
        status = str(item.get("status") or MISSING).upper()
        if status not in STATUSES:
            status = MISSING
        quote = str(item.get("quote") or "").strip()
        verdict = PointVerdict(point.key, point.description, point.importance, status, quote)
        if status != MISSING:
            if not quote:
                verdict.status, verdict.adjusted = MISSING, "no_quote"
            elif not quote_is_verified(quote, answer):
                verdict.status, verdict.adjusted = MISSING, "unverified_quote"
        if verdict.status == COVERED:
            normalized_quote = normalize_for_match(quote)
            if _NEGATION.search(normalized_quote) and not _NEGATION.search(
                normalize_for_match(point.description)
            ):
                # "em không dùng index" quoted as covering "dùng index".
                verdict.status, verdict.adjusted = PARTIAL, "negation_in_quote"
            elif point.importance == CORE and normalized_quote in credited_core_quotes:
                verdict.status, verdict.adjusted = PARTIAL, "quote_reused"
            if point.importance == CORE:
                credited_core_quotes.add(normalized_quote)
        if verdict.status == MISSING:
            verdict.quote = ""
        verdicts.append(verdict)
    return verdicts


# Order for taking the median label: a wrong statement ranks below silence.
_LABEL_RANK = {INCORRECT: 0, MISSING: 1, PARTIAL: 2, COVERED: 3}


def majority_verdicts(votes: list[list[PointVerdict]]) -> list[PointVerdict]:
    """Combine independent labellings point by point.

    Each point takes the median of its labels, which is the majority label
    whenever most votes agree; with an even count it takes the lower middle.
    The quote comes from a vote that gave that label, so it stays verified.
    """
    if not votes:
        raise ChecklistOutputError("No grader votes")
    combined: list[PointVerdict] = []
    for index, first in enumerate(votes[0]):
        labels = sorted((vote[index] for vote in votes), key=lambda v: _LABEL_RANK[v.status])
        chosen = labels[(len(labels) - 1) // 2]
        combined.append(
            PointVerdict(first.key, first.description, first.importance, chosen.status, chosen.quote, chosen.adjusted)
        )
    return combined


def checklist_score(verdicts: list[PointVerdict]) -> float:
    """Fixed formula from verified labels; the grader never supplies the number."""
    core = [v for v in verdicts if v.importance == CORE]
    bonus = [v for v in verdicts if v.importance == BONUS]
    if not core:
        # Only bonus points: treat them as the checklist.
        core, bonus = bonus, []
    core_ratio = sum(CREDIT[v.status] for v in core) / len(core)
    if bonus:
        score = CORE_MAX_WITH_BONUS * core_ratio + (10.0 - CORE_MAX_WITH_BONUS) * (
            sum(CREDIT[v.status] for v in bonus) / len(bonus)
        )
    else:
        score = 10.0 * core_ratio
    score -= INCORRECT_PENALTY * sum(1 for v in verdicts if v.status == INCORRECT)
    return round(max(0.0, min(10.0, score)), 1)


def checklist_strengths_and_weaknesses(
    verdicts: list[PointVerdict], *, is_vi: bool
) -> tuple[list[str], list[str]]:
    """Strengths and weaknesses written from the checklist, not by the grader."""
    strengths: list[str] = []
    weaknesses: list[str] = []
    for v in verdicts:
        if v.status == COVERED:
            strengths.append(v.description)
        elif v.status == PARTIAL:
            strengths.append(f"{'Nêu một phần' if is_vi else 'Partly covered'}: {v.description}")
        elif v.status == INCORRECT:
            weaknesses.append(f"{'Nêu chưa đúng' if is_vi else 'Stated incorrectly'}: {v.description}")
        elif v.importance == CORE:
            weaknesses.append(f"{'Chưa nêu' if is_vi else 'Not covered'}: {v.description}")
    return strengths, weaknesses
