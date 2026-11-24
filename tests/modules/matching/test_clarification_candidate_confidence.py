"""A low-confidence clarification candidate must survive the response model.

``ClarificationCandidate.confidence`` was floored at 0.75, which encoded the
assumption that every candidate had cleared a 0.75 gate.  Two things broke that
assumption - the gate's threshold is configurable and can be 0, and a
requirement the gate declines still leaves with a template plan at confidence 0
so the reader is never shown "cần xác nhận" with nothing to answer - and the
floor then rejected those plans and failed the entire
``clarifications/prepare-by-ids`` response with a 500.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.modules.matching.clarifications.models import ClarificationCandidate


@pytest.mark.parametrize("confidence", [0.0, 0.3, 0.74, 0.75, 1.0])
def test_any_confidence_the_gate_produced_is_serializable(confidence):
    candidate = ClarificationCandidate(
        requirementId="req-1",
        missingDimension="other",
        confidence=confidence,
    )

    assert candidate.confidence == confidence


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_a_confidence_outside_zero_to_one_is_still_refused(confidence):
    with pytest.raises(ValidationError):
        ClarificationCandidate(
            requirementId="req-1",
            missingDimension="other",
            confidence=confidence,
        )
