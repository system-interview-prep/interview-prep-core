"""Measured noise for the progress page (nghiên cứu 20/10, PROGRESS_EVIDENCE_RESEARCH).

A skill's score in one session moves for reasons that are not the candidate:
different questions, the grader, the day. Calling a change "progress" needs to
know how big that movement normally is. ``scripts/progress_calibration.py``
measures it from stored data and writes one row to ``progress_calibration``;
the progress page reads the newest row. Until a row exists, conservative
defaults are used and the page says the noise has not been measured yet.

Terms (Hopkins, "A New View of Statistics"):

* ``typical_error``: how much one skill's session score differs between two
  close sessions of the same person at the same tier, SD(diff) / sqrt(2).
* ``between_sd``: spread of skill scores between people.
* ``swc``: smallest worthwhile change, 0.2 x between_sd.
* ``grading_sd``: spread of the grader's score when the same answer is graded
  again (the verdict cache hides this in production; the script bypasses it).
* ``tier_offsets``: points to add to a score at a tier to compare it with the
  at-level tier (a challenge question scores lower for the same ability).

Pure functions here; database access is limited to reading and writing the
calibration row.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Defaults before anything is measured: on the cautious side, so the page
# claims progress less often rather than more (research note: about 1 point is
# needed with these values when comparing 3 sessions with 3 sessions).
DEFAULT_TYPICAL_ERROR = 1.2
DEFAULT_BETWEEN_SD = 1.5
SWC_FACTOR = 0.2
# Two sessions further apart than this measure learning, not noise.
PAIR_MAX_GAP_DAYS = 14
MIN_PAIRS = 10
MIN_USERS_PER_SKILL = 3
MIN_POINTS_PER_TIER = 20
TIERS = ("basic", "standard", "challenge")


@dataclass(frozen=True)
class SkillSessionPoint:
    """One skill's mean score in one session of one person."""

    user_id: str
    concept: str
    tier: str
    started_at: datetime
    score: float
    grader_version: str | None = None


@dataclass
class Calibration:
    typical_error: float = DEFAULT_TYPICAL_ERROR
    between_sd: float = DEFAULT_BETWEEN_SD
    grading_sd: float | None = None
    tier_offsets: dict[str, float] = field(default_factory=dict)
    measured_at: datetime | None = None
    grader_version: str | None = None
    sample: dict[str, Any] = field(default_factory=dict)

    @property
    def measured(self) -> bool:
        return self.measured_at is not None

    @property
    def swc(self) -> float:
        return round(SWC_FACTOR * self.between_sd, 3)

    def offset(self, tier: str) -> float:
        return float(self.tier_offsets.get(tier, 0.0))

    @property
    def tier_adjusted(self) -> bool:
        """Scores of different tiers can be compared only once offsets were measured."""
        return bool(self.tier_offsets)

    def to_public(self) -> dict[str, Any]:
        return {
            "measured": self.measured,
            "measuredAt": self.measured_at.isoformat() if self.measured_at else None,
            "typicalError": round(self.typical_error, 2),
            "swc": self.swc,
            "gradingSd": round(self.grading_sd, 2) if self.grading_sd is not None else None,
            "tierOffsets": {k: round(v, 2) for k, v in self.tier_offsets.items()},
            "tierAdjusted": self.tier_adjusted,
        }


# --------------------------------------------------------------------------- #
# Measurements (pure)
# --------------------------------------------------------------------------- #


def typical_error(
    points: list[SkillSessionPoint], *, max_gap_days: int = PAIR_MAX_GAP_DAYS
) -> tuple[float | None, int]:
    """SD of the difference between consecutive close sessions, / sqrt(2).

    Pairs are taken within one person, skill, tier and grader version, at most
    ``max_gap_days`` apart. Returns (None, n) below ``MIN_PAIRS`` pairs.
    """
    groups: dict[tuple[str, str, str, str | None], list[SkillSessionPoint]] = defaultdict(list)
    for point in points:
        groups[(point.user_id, point.concept, point.tier, point.grader_version)].append(point)
    diffs: list[float] = []
    for items in groups.values():
        items.sort(key=lambda p: p.started_at)
        for earlier, later in zip(items, items[1:], strict=False):
            if (later.started_at - earlier.started_at).days <= max_gap_days:
                diffs.append(later.score - earlier.score)
    if len(diffs) < MIN_PAIRS:
        return None, len(diffs)
    return statistics.pstdev(diffs) / math.sqrt(2), len(diffs)


def between_person_sd(points: list[SkillSessionPoint]) -> tuple[float | None, int]:
    """Spread of people's mean score on the same skill, pooled over skills."""
    per_skill: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for point in points:
        per_skill[point.concept][point.user_id].append(point.score)
    weighted, weight = 0.0, 0
    for users in per_skill.values():
        if len(users) < MIN_USERS_PER_SKILL:
            continue
        means = [statistics.mean(scores) for scores in users.values()]
        weighted += statistics.pvariance(means) * len(means)
        weight += len(means)
    if weight == 0:
        return None, 0
    return math.sqrt(weighted / weight), weight


def tier_offsets(points: list[SkillSessionPoint]) -> dict[str, float]:
    """Mean at-level score minus mean tier score, within a skill, weighted by points.

    Only tiers with ``MIN_POINTS_PER_TIER`` points in skills that also have
    at-level points get an offset; the at-level tier is the reference (0).
    """
    per_skill: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for point in points:
        per_skill[point.concept][point.tier].append(point.score)
    sums: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for tiers in per_skill.values():
        reference = tiers.get("standard")
        if not reference:
            continue
        ref_mean = statistics.mean(reference)
        for tier, scores in tiers.items():
            if tier == "standard":
                continue
            sums[tier] += (ref_mean - statistics.mean(scores)) * len(scores)
            counts[tier] += len(scores)
    offsets = {
        tier: round(sums[tier] / counts[tier], 2) for tier in counts if counts[tier] >= MIN_POINTS_PER_TIER
    }
    if offsets:
        offsets["standard"] = 0.0
    return offsets


def grading_sd(repeats: list[list[float]]) -> tuple[float | None, int]:
    """Pooled SD of repeated grades of the same answers."""
    variances = [statistics.variance(scores) for scores in repeats if len(scores) >= 2]
    if not variances:
        return None, 0
    return math.sqrt(statistics.mean(variances)), len(variances)


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


async def load_calibration(db: AsyncSession) -> Calibration:
    """Newest calibration row, or the defaults (also when the migration is missing)."""
    try:
        row = (
            await db.execute(
                text(
                    """
                    SELECT measured_at, grader_version, grading_sd, typical_error, between_sd,
                           tier_offsets, sample
                    FROM progress_calibration ORDER BY measured_at DESC LIMIT 1
                    """
                )
            )
        ).mappings().one_or_none()
    except Exception:  # noqa: BLE001 - migration 20261020_0035 not applied yet
        await db.rollback()
        return Calibration()
    if not row:
        return Calibration()
    offsets = row["tier_offsets"]
    if isinstance(offsets, str):
        offsets = json.loads(offsets or "{}")
    sample = row["sample"]
    if isinstance(sample, str):
        sample = json.loads(sample or "{}")
    return Calibration(
        typical_error=float(row["typical_error"]) if row["typical_error"] else DEFAULT_TYPICAL_ERROR,
        between_sd=float(row["between_sd"]) if row["between_sd"] else DEFAULT_BETWEEN_SD,
        grading_sd=float(row["grading_sd"]) if row["grading_sd"] is not None else None,
        tier_offsets={str(k): float(v) for k, v in (offsets or {}).items() if k in TIERS},
        measured_at=row["measured_at"],
        grader_version=row["grader_version"],
        sample=sample or {},
    )


async def save_calibration(db: AsyncSession, calibration: Calibration, *, notes: str = "") -> None:
    await db.execute(
        text(
            """
            INSERT INTO progress_calibration
            (grader_version, grading_sd, typical_error, between_sd, tier_offsets, sample, notes)
            VALUES (:gv, :gsd, :te, :bsd, CAST(:offsets AS jsonb), CAST(:sample AS jsonb), :notes)
            """
        ),
        {
            "gv": calibration.grader_version,
            "gsd": calibration.grading_sd,
            "te": calibration.typical_error,
            "bsd": calibration.between_sd,
            "offsets": json.dumps(calibration.tier_offsets),
            "sample": json.dumps(calibration.sample, ensure_ascii=False),
            "notes": notes,
        },
    )
    await db.commit()
