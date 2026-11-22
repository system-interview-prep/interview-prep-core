"""What the judge is allowed to say, and the shape it has to say it in.

Every field here is a closed choice.  The model never writes a verdict in
prose, never names a span by its text and never produces a number the rule
side does not check again: it picks a relation from four values, points at
candidate spans by the ids it was handed, and copies a quote that must be
found verbatim inside that span.  Anything else is dropped before it reaches
an evaluator, and the drop is traced.

``not_met`` is deliberately absent.  A rejection is still the rule side's
decision, made only when it has confirmed the CV is complete and holds nothing
(``legacy_rules._absence_is_conclusive``).  The judge can raise a verdict with
grounded evidence or leave it where the rules put it; it cannot lower it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from src.modules.user_cvs.schemas import CanonicalModel

PROMPT_VERSION = "entailment-v1"
GATE_PROMPT_VERSION = "gate-v1"

Relation = Literal["satisfies", "partially", "unrelated", "unsure"]
# What a posting's line is, as the gate judge reads it.  Mirrors the rule
# classes in ``requirement_gating`` minus ``vacuous``, which stays a rule
# decision: a negation is a matter of syntax, not of reading.
GateRelation = Literal["objective", "soft_signal", "non_assessable", "optional", "unsure"]

# Why a requirement has no reading, or why a span the model picked was dropped.
FallbackReason = Literal[
    "judge_unavailable",
    "no_candidates",
    "transport",
    "schema",
    "no_valid_span",
]


class SelectedSpan(CanonicalModel):
    """One candidate span the model pointed at, with the words it relied on."""

    evidence_id: str = Field(min_length=1)
    quote: str = Field(min_length=3, max_length=600)


class EntailmentReading(CanonicalModel):
    """The judge's grounded reading of one requirement against one CV."""

    requirement_id: str = Field(min_length=1)
    relation: Relation
    # Only spans that survived the id and quote checks.  ``satisfies`` with an
    # empty list is impossible: the judge downgrades it to ``unsure``.
    selected: list[SelectedSpan] = Field(default_factory=list)
    dropped: dict[str, str] = Field(default_factory=dict)


class ReadingSet(CanonicalModel):
    """Every reading made for one match, keyed by requirement id.

    Built once before the evaluators run and handed to them read-only, so the
    evaluators stay synchronous and deterministic for a given set of readings.
    """

    prompt_version: str = PROMPT_VERSION
    entailments: dict[str, EntailmentReading] = Field(default_factory=dict)
    fallbacks: dict[str, FallbackReason] = Field(default_factory=dict)

    def reading_for(self, requirement_id: str) -> EntailmentReading | None:
        return self.entailments.get(requirement_id)


class GateReading(CanonicalModel):
    """The gate judge's reading of one posting line, with the words it relied on.

    Only a demotion can come out of this - ``requirement_gating`` applies a
    reading solely to lower ``objective`` to one of the non-gating classes,
    never the reverse - so a reading that cannot point at the wording that
    makes the line soft or unassessable is ``unsure``.
    """

    raw_label: str = Field(min_length=1)
    relation: GateRelation
    cue_quote: str | None = None
