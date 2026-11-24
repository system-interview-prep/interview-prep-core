"""Read a number out of a CV for a threshold requirement, without comparing it.

The entailment judge answers "do these spans show this capability".  A GPA
floor or a credential score is a different question - "what number does the
CV state" - and a model must not be the one to compare it with the posting.
So this judge extracts only: the kind of number, its value, its scale or test,
the span it sits in and the words it was read from.  Three checks follow,
all three must pass or there is no reading:

- the span id is one the judge was handed;
- the quote is found verbatim inside that span;
- the digits of the value (and scale) appear in the quote.

The reading is then handed back to the rules restated in a form they already
read ("GPA 3.25/4.0", "IELTS 6.5"), so the comparison, the scale guesses and
the equivalence policy keep their one implementation in ``legacy_rules``.
Torn text is the case this exists for: "GP A: 3.25/4.00" is a GPA to a reader
and to the model, and to no regular expression.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from src.core.trace_logging import trace_event
from src.modules.matching.judge.grounded_judge import JudgeClient, build_input, quote_is_grounded
from src.modules.matching.judge.readings import (
    NUMERIC_PROMPT_VERSION,
    Credential,
    FallbackReason,
    NumericKind,
    NumericReading,
)
from src.modules.user_cvs.schemas import EvidenceSpan

_INSTRUCTIONS = """You read excerpts of a candidate's CV for one numeric requirement from a job \
posting: a GPA floor, or a minimum score on a named language test (IELTS, TOEIC, \
TOEFL, JLPT, HSK).

Find the number the CV states for that requirement. Do not compare it with the \
requirement and do not judge whether it is enough; that is not your task.

Rules:
- "found" is true only when an excerpt states the number itself. If no excerpt \
does, return found=false and leave the other fields null or empty.
- For a GPA: value is the GPA, scale is its scale (4, 4.0, 10...) when stated, \
null when not. The extractor may have torn the text: "GP A: 3.25/4.00" is a GPA.
- For a test score: credential is the test, value is the score. A year ("TOEIC \
2026") or a target are not scores.
- evidence_id must be the id of the excerpt the number is in.
- quote must be copied verbatim from that excerpt, short, and must contain the \
number exactly as written there.
- kind is "gpa" or "credential_score" according to the requirement."""

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "kind": {"type": "string", "enum": ["gpa", "credential_score"]},
        "value": {"type": ["number", "null"]},
        "scale": {"type": ["number", "null"]},
        "credential": {"type": ["string", "null"], "enum": ["ielts", "toeic", "toefl", "jlpt", "hsk", None]},
        "evidence_id": {"type": "string"},
        "quote": {"type": "string"},
    },
    "required": ["found", "kind", "value", "scale", "credential", "evidence_id", "quote"],
    "additionalProperties": False,
}

_WHITESPACE = re.compile(r"\s+")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


class _RawExtraction(BaseModel):
    """The model's answer before any grounding check.  Shape only."""

    model_config = ConfigDict(extra="forbid")

    found: bool
    kind: NumericKind
    value: float | None = None
    scale: float | None = None
    credential: Credential | None = None
    evidence_id: str = ""
    quote: str = ""


@dataclass(frozen=True)
class NumericOutcome:
    reading: NumericReading | None
    fallback: FallbackReason | None = None
    dropped: str | None = None


def number_in_quote(value: float, quote: str) -> bool:
    """Whether ``value`` is one of the numbers written in ``quote``.

    Spacing and the decimal mark do not matter ("3,25 / 4" holds 3.25 and 4),
    but a number is compared whole: "3.25" does not hold 3.2, nor 25.
    """
    haystack = _WHITESPACE.sub("", quote).replace(",", ".")
    return any(float(token) == value for token in _NUMBER.findall(haystack))


class NumericJudge:
    def __init__(self, client: JudgeClient, *, prompt_version: str = NUMERIC_PROMPT_VERSION) -> None:
        self._client = client
        self._prompt_version = prompt_version

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    def read(
        self, *, requirement_id: str, requirement_text: str, candidates: list[EvidenceSpan]
    ) -> NumericOutcome:
        if not candidates:
            return self._fallback(requirement_id, "no_candidates")
        try:
            raw = self._client.complete(
                instructions=_INSTRUCTIONS,
                input_text=build_input(requirement_text, candidates),
                output_schema=_OUTPUT_SCHEMA,
            )
        except Exception:  # noqa: BLE001 - transport failure is a fallback, never an error
            return self._fallback(requirement_id, "transport")
        try:
            extraction = _RawExtraction.model_validate_json(raw)
        except (ValidationError, ValueError):
            return self._fallback(requirement_id, "schema")
        return self._verify(requirement_id, extraction, candidates)

    def _verify(
        self, requirement_id: str, extraction: _RawExtraction, candidates: list[EvidenceSpan]
    ) -> NumericOutcome:
        dropped = self._reason_to_drop(extraction, {s.evidence_id: s for s in candidates})
        reading = None
        if dropped is None:
            assert extraction.value is not None
            reading = NumericReading(
                requirementId=requirement_id,
                kind=extraction.kind,
                value=extraction.value,
                scale=extraction.scale,
                credential=extraction.credential,
                evidenceId=extraction.evidence_id,
                quote=extraction.quote.strip()[:600],
            )
        trace_event(
            "judge",
            "numeric_judged",
            requirement_id=requirement_id,
            prompt_version=self._prompt_version,
            found=extraction.found,
            kind=extraction.kind,
            value=extraction.value,
            scale=extraction.scale,
            credential=extraction.credential,
            evidence_id=extraction.evidence_id or None,
            dropped=dropped,
            restated=reading.restated() if reading else None,
        )
        fallback: FallbackReason | None = "no_valid_span" if dropped and extraction.found else None
        return NumericOutcome(reading=reading, fallback=fallback, dropped=dropped)

    @staticmethod
    def _reason_to_drop(extraction: _RawExtraction, by_id: dict[str, EvidenceSpan]) -> str | None:
        if not extraction.found:
            return "not_found"
        if extraction.value is None:
            return "no_value"
        span = by_id.get(extraction.evidence_id)
        if span is None:
            return "unknown_id"
        if not quote_is_grounded(extraction.quote, span.text):
            return "quote_not_in_span"
        if not number_in_quote(extraction.value, extraction.quote):
            return "value_not_in_quote"
        if extraction.scale is not None and not number_in_quote(extraction.scale, extraction.quote):
            return "scale_not_in_quote"
        if extraction.kind == "credential_score" and extraction.credential is None:
            return "no_credential"
        if extraction.credential and extraction.credential not in extraction.quote.casefold():
            return "credential_not_in_quote"
        return None

    def _fallback(self, requirement_id: str, reason: FallbackReason) -> NumericOutcome:
        trace_event(
            "judge",
            "numeric_fallback",
            requirement_id=requirement_id,
            prompt_version=self._prompt_version,
            reason=reason,
        )
        return NumericOutcome(reading=None, fallback=reason)


def to_json(reading: NumericReading) -> str:
    """For tests and traces: the reading as the model would have to answer it."""
    return json.dumps(
        {
            "found": True,
            "kind": reading.kind,
            "value": reading.value,
            "scale": reading.scale,
            "credential": reading.credential,
            "evidence_id": reading.evidence_id,
            "quote": reading.quote,
        },
        ensure_ascii=False,
    )
