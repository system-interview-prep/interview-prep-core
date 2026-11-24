"""Factor scores must stay inside [0, 1] despite floating-point rounding.

``FactorResult`` constrains ``raw_score`` and ``reliability`` to ``le=1``. Both
are weighted means of values that are themselves in [0, 1], so both are bounded
by 1 mathematically - but the numerator and the denominator sum the same weights
in different orders, and the quotient can land one unit above. That reached
production as a 500 on POST /api/v1/matching/match-ids with
``Input should be less than or equal to 1 [input_value=1.0000000000000002]``.
"""

from __future__ import annotations

import pytest

from src.modules.matching.application.facade import _unit
from src.modules.matching.domain.schemas import FactorResult


def test_an_ulp_above_one_is_trimmed() -> None:
    assert _unit(1.0000000000000002) == 1.0


def test_an_ulp_below_zero_is_trimmed() -> None:
    assert _unit(-1e-17) == 0.0


@pytest.mark.parametrize("value", [0.0, 0.25, 0.5, 0.999, 1.0])
def test_values_already_in_range_are_returned_unchanged(value: float) -> None:
    """The clamp must not perturb any legitimate score."""
    assert _unit(value) == value


def test_the_trimmed_value_satisfies_the_published_contract() -> None:
    """The exact value that produced the 500 now constructs successfully."""
    with pytest.raises(ValueError):
        FactorResult(
            factor="requirement_coverage",
            status="scored",
            rawScore=1.0,
            reliability=1.0000000000000002,
            policyWeight=0.6,
            effectiveWeight=0.0,
        )

    result = FactorResult(
        factor="requirement_coverage",
        status="scored",
        rawScore=_unit(1.0000000000000002),
        reliability=_unit(1.0000000000000002),
        policyWeight=0.6,
        effectiveWeight=0.0,
    )
    assert result.raw_score == 1.0
    assert result.reliability == 1.0
