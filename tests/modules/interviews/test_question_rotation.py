"""A new session for the same CV and JD rotates questions; demo sessions keep them fixed."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.modules.interviews.planning.question_selector import _candidate_rank, _question_exposure
from tests.modules.interviews.test_question_selector import candidate


def _order(candidates, exposure):
    ranked = sorted(
        candidates,
        key=lambda c: _candidate_rank(
            c, difficulty="intermediate", locale="vi-VN", salt="s1", exposure=exposure
        ),
    )
    return [c.question_version_id for c in ranked]


def test_unseen_question_goes_before_a_more_relevant_seen_one() -> None:
    seen = candidate(question_version_id="seen", relevance=1.0)
    fresh = candidate(question_version_id="fresh", relevance=0.6)
    assert _order([seen, fresh], {}) == ["seen", "fresh"]
    assert _order([seen, fresh], {"seen": "2026-10-09T10:00:00+00:00"}) == ["fresh", "seen"]


def test_when_every_question_was_seen_the_oldest_comes_back_first() -> None:
    a, b, c = (candidate(question_version_id=q, relevance=1.0) for q in ("a", "b", "c"))
    exposure = {
        "a": "2026-10-09T12:00:00+00:00",
        "b": "2026-10-07T12:00:00+00:00",
        "c": "2026-10-08T12:00:00+00:00",
    }
    assert _order([a, b, c], exposure) == ["b", "c", "a"]


def test_rotation_never_overrides_difficulty_fit() -> None:
    on_level = candidate(question_version_id="on-level", difficulty_band="intermediate")
    off_level = candidate(question_version_id="off-level", difficulty_band="advanced")
    assert _order([on_level, off_level], {"on-level": "2026-10-09T10:00:00+00:00"})[0] == "on-level"


@pytest.mark.asyncio
async def test_exposure_maps_questions_the_candidate_was_asked_in_other_sessions() -> None:
    result = MagicMock()
    result.mappings.return_value.all.return_value = [
        {"question_version_id": "q1", "last_seen": datetime(2026, 10, 9, 10, tzinfo=UTC)},
    ]
    db = AsyncMock()
    db.execute.return_value = result

    exposure = await _question_exposure(db, {"id": "s2", "duration_minutes": 25})

    assert exposure == {"q1": "2026-10-09T10:00:00+00:00"}
    sql = str(db.execute.call_args.args[0])
    assert "s.id <> :session_id" in sql and "t.status <> 'PLANNED'" in sql


@pytest.mark.asyncio
async def test_demo_sessions_keep_fixed_questions() -> None:
    db = AsyncMock()
    assert await _question_exposure(db, {"id": "s3", "duration_minutes": 3}) == {}
    db.execute.assert_not_called()
