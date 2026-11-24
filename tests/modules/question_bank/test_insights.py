"""What the bank does with suggestions: who may decide, calibration rows."""

from src.modules.question_bank import insights


def test_who_decides_what() -> None:
    assert insights.can_decide("REVISE", "ACCEPT", {"QUESTION_AUTHOR"})
    assert not insights.can_decide("RETIRE", "ACCEPT", {"QUESTION_AUTHOR"})
    assert insights.can_decide("RETIRE", "ACCEPT", {"ADMIN"})
    assert insights.can_decide("VARIANT", "DISMISS", {"ADMIN"})
    assert not insights.can_decide("VARIANT", "DISMISS", {"QUESTION_AUTHOR"})
    assert not insights.can_decide("REVISE", "DISMISS", {"QUESTION_AUTHOR"})
    # Retired roles (folded into ADMIN on 28/10) decide nothing.
    assert not insights.can_decide("RETIRE", "ACCEPT", {"QUESTION_BANK_ADMIN"})
    assert not insights.can_decide("VARIANT", "DISMISS", {"QUESTION_REVIEWER"})
    assert insights.can_decide("DUPLICATE", "ACCEPT", {"ADMIN"})


def test_calibration_rows_store_scores_on_a_zero_to_one_scale() -> None:
    row = insights.calibration_row(
        "6f1c1b7e-8a7b-4b54-9a52-1c0e7b0d2a11",
        {
            "graded": 12,
            "meanScore": 6.5,
            "scoreStdDev": 1.25,
            "skipRate": 0.1,
            "overtimeRate": None,
            "empiricalDifficulty": 0.65,
            "discrimination": 0.31,
            "asked": 14,
            "sessions": 12,
            "lowData": True,
        },
        "auto-20261026",
    )
    assert row["mean_score"] == 0.65 and row["score_stddev"] == 0.125
    assert row["timeout_rate"] == 0 and row["discrimination"] == 0.31
    assert row["slice_metrics"]["lowData"] is True
    assert insights.calibration_row("6f1c1b7e-8a7b-4b54-9a52-1c0e7b0d2a11", {"graded": 0}, "auto") is None
