"""Comparing English credentials with each other, through CEFR.

A posting that asks for "IELTS 6.0 hoặc tương đương" is asking for a level of
English, not for one particular test.  The evaluator used to compare the test's
*name* with a string equality, so a candidate holding TOEIC 900 - comfortably
above the level asked for - was reported as having no evidence at all.

Every number below is a published alignment from the body that owns the test,
not an interpolation between charts:

* IELTS: the official IELTS/British Council "IELTS and the CEFR" bands.
* TOEIC Listening & Reading (out of 990): ETS, "Mapping the TOEIC Tests on the
  CEFR".  The Speaking & Writing test is a different 0-400 scale and is not
  covered here.
* TOEFL iBT (out of 120): the ETS iBT-to-CEFR alignment.

The comparison that results is at CEFR granularity, which is coarser than a raw
score: IELTS 6.0 and IELTS 6.5 are both B2, so a cross-test comparison cannot
separate them.  That is the correct resolution for the question being asked -
"hoặc tương đương" is itself a statement about levels - but it is the reason a
same-test comparison must keep using the raw score and never route through
here.
"""

from __future__ import annotations

import re

CEFR_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")
_CEFR_RANK = {level: index for index, level in enumerate(CEFR_LEVELS)}

# Credential -> the score range that is actually on that test's scale.  A
# number outside it is not a score: "TOEIC 2026" is a year, and reading it as a
# result let any threshold pass.
_SCORE_RANGE: dict[str, tuple[float, float]] = {
    "ielts": (0.0, 9.0),
    "toeic": (10.0, 990.0),
    "toefl": (0.0, 120.0),
}

# Credential -> ascending (lowest score in the band, CEFR level).  A score below
# the first floor is off the bottom of the published alignment and is left
# unplaced rather than guessed.
_CEFR_BANDS: dict[str, tuple[tuple[float, str], ...]] = {
    "ielts": ((4.0, "B1"), (5.5, "B2"), (7.0, "C1"), (8.5, "C2")),
    "toeic": ((120.0, "A1"), (225.0, "A2"), (550.0, "B1"), (785.0, "B2"), (945.0, "C1")),
    "toefl": ((42.0, "B1"), (72.0, "B2"), (95.0, "C1"), (115.0, "C2")),
}

# A CEFR level stated in words needs an anchor in front of it, or every "B1" in
# a sentence about something else becomes an English result.
_CEFR_LEVEL_RE = re.compile(
    r"\b(?:cefr|khung\s+chau\s+au|khung\s+tham\s+chieu\s+chau\s+au|english|tieng\s+anh)"
    r"(?:\s+(?:level|trinh\s+do|muc))?\s*(?P<level>[abc][12])\b",
    re.I,
)


# TOEIC Speaking & Writing is a separate 0-400 test.  Its results look like
# Listening & Reading results and are not.
_TOEIC_SPEAKING_WRITING_RE = re.compile(r"\b(?:s\s+w|speaking\s+(?:and\s+|&\s*)?writing|noi\s+viet)\b", re.I)


def scale_is_unambiguous(credential: str, text: str) -> bool:
    """Whether the surrounding text agrees with the scale this table assumes.

    Reading a TOEIC Speaking & Writing 350 against the 0-990 Listening &
    Reading bands would place a strong result at A2 and reject the candidate
    for it.  Where the variant is named and is not the one described here, the
    honest answer is that the score cannot be placed at all.
    """

    if credential.strip().casefold() != "toeic":
        return True
    return _TOEIC_SPEAKING_WRITING_RE.search(text) is None


def is_english_credential(credential: str) -> bool:
    """Whether this test measures English, and so may be compared with another.

    JLPT and HSK sit in the same requirement field and the same regex, and a
    JLPT N2 is not a statement about English.  Nothing outside this set is ever
    converted.
    """

    return credential.strip().casefold() in _CEFR_BANDS


def score_is_on_scale(credential: str, score: float) -> bool:
    """Whether a number is a plausible result on that test's own scale."""

    bounds = _SCORE_RANGE.get(credential.strip().casefold())
    if bounds is None:
        return True
    low, high = bounds
    return low <= score <= high


def cefr_level_for_score(credential: str, score: float) -> str | None:
    """The published CEFR band for a score, or ``None`` when it has none."""

    bands = _CEFR_BANDS.get(credential.strip().casefold())
    if bands is None or not score_is_on_scale(credential, score):
        return None
    level: str | None = None
    for floor, band in bands:
        if score >= floor:
            level = band
        else:
            break
    return level


def cefr_level_in_text(text: str) -> str | None:
    """A CEFR level a CV states outright, as in "English: CEFR B1"."""

    match = _CEFR_LEVEL_RE.search(text)
    return match.group("level").upper() if match else None


def cefr_meets(candidate_level: str, required_level: str) -> bool:
    """Whether the candidate's band is at or above the one required."""

    return _CEFR_RANK[candidate_level.upper()] >= _CEFR_RANK[required_level.upper()]


def cefr_rank(level: str) -> int:
    """Position in :data:`CEFR_LEVELS`, for picking a candidate's best result."""

    return _CEFR_RANK[level.upper()]
