"""How far a question version has been checked: its trust tier.

``APPROVED`` used to mean one thing, but the seeded bank is approved by
``system-approver`` -- nobody read those questions -- and it ranked equal to a
question an admin reviewed. The tier separates the cases, and is derived from
data the bank already keeps, so there is no column to keep in sync:

====  ==========  ==========================================================
tier  name        condition
====  ==========  ==========================================================
0     generated   not approved yet (an AI draft still in review)
1     initial     approved, but no person recorded an APPROVE review
2     reviewed    approved and a person recorded an APPROVE review
3     calibrated  reviewed, answered often enough, and no open finding
====  ==========  ==========================================================

A person is any reviewer id that is not a ``system-`` actor (the seed author,
the seed approver, the question generator). Tier 3 is not stored either: a new
finding from real interviews (``question_bank_suggestions``) drops the question
back to tier 2 until someone deals with it. The stored status ``CALIBRATED``
is honoured as tier 3 for versions someone set by hand.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SYSTEM_ACTOR_PREFIX = "system-"
VERIFICATION_REVIEW_TYPE = "VERIFICATION"
# Graded answers a version needs before its item statistics count as
# calibration. A working threshold, not a statistical one: with fewer answers
# the analysis itself marks the statistics as low-data.
CALIBRATION_MIN_SAMPLE = 20

GENERATED, INITIAL, REVIEWED, CALIBRATED = 0, 1, 2, 3
TIER_NAMES = {GENERATED: "generated", INITIAL: "initial", REVIEWED: "reviewed", CALIBRATED: "calibrated"}
TIER_BY_NAME = {name: tier for tier, name in TIER_NAMES.items()}

_PREFIX_LENGTH = len(SYSTEM_ACTOR_PREFIX)


def trust_tier_sql(alias: str = "qv") -> str:
    """SQL expression for the tier of the version row aliased ``alias``.

    ``alias`` is a code constant, never user input. No bound parameter is used,
    so the expression can be dropped into any query.
    """
    human_review = (
        "EXISTS (SELECT 1 FROM question_reviews tr "
        f"WHERE tr.question_version_id = {alias}.id AND tr.decision = 'APPROVE' "
        f"AND left(tr.reviewer_id, {_PREFIX_LENGTH}) <> '{SYSTEM_ACTOR_PREFIX}')"
    )
    calibrated = (
        "EXISTS (SELECT 1 FROM question_calibrations tc "
        f"WHERE tc.question_version_id = {alias}.id AND tc.sample_size >= {CALIBRATION_MIN_SAMPLE}) "
        "AND NOT EXISTS (SELECT 1 FROM question_bank_suggestions ts "
        f"WHERE ts.question_id = {alias}.question_id AND ts.status = 'OPEN')"
    )
    return (
        "(CASE "
        f"WHEN {alias}.status NOT IN ('APPROVED', 'CALIBRATED') THEN {GENERATED} "
        f"WHEN NOT {human_review} THEN {INITIAL} "
        f"WHEN {alias}.status = 'CALIBRATED' OR ({calibrated}) THEN {CALIBRATED} "
        f"ELSE {REVIEWED} END)"
    )


def tier_name(tier: Any) -> str:
    try:
        return TIER_NAMES[int(tier)]
    except (KeyError, TypeError, ValueError):
        return TIER_NAMES[GENERATED]


async def trust_tiers(db: AsyncSession, version_ids: list[str]) -> dict[str, str]:
    """Current tier name of each question version id that still exists."""
    ids = [str(item) for item in dict.fromkeys(version_ids) if item]
    if not ids:
        return {}
    rows = await db.execute(
        text(
            f"SELECT CAST(qv.id AS TEXT) AS id, {trust_tier_sql('qv')} AS trust_tier "
            "FROM interview_question_versions qv WHERE CAST(qv.id AS TEXT) = ANY(:ids)"
        ),
        {"ids": ids},
    )
    return {row["id"]: tier_name(row["trust_tier"]) for row in rows.mappings().all()}


def trust_summary(tiers: list[str]) -> dict[str, int]:
    """``{"total", "reviewed"}`` for a list of tier names: reviewed counts tier 2 and 3."""
    reviewed = sum(TIER_BY_NAME.get(name, GENERATED) >= REVIEWED for name in tiers)
    return {"total": len(tiers), "reviewed": reviewed}


__all__ = [
    "CALIBRATED",
    "CALIBRATION_MIN_SAMPLE",
    "GENERATED",
    "INITIAL",
    "REVIEWED",
    "SYSTEM_ACTOR_PREFIX",
    "TIER_BY_NAME",
    "TIER_NAMES",
    "VERIFICATION_REVIEW_TYPE",
    "tier_name",
    "trust_summary",
    "trust_tier_sql",
    "trust_tiers",
]
