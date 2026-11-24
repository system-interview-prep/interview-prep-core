from datetime import UTC, datetime, timedelta

import pytest

from src.modules.interviews.application.runtime.session_deadline import chat_deadline


def session(**extra):
    return {
        "mode": "text",
        "experience_type": "interview_chat",
        "started_at": "2026-11-07T09:00:00+07:00",
        "duration_minutes": 5,
        **extra,
    }


def test_five_minutes_means_five_minutes_from_the_opening_question():
    assert chat_deadline(session(), has_started=True) == datetime(2026, 11, 7, 2, 5, tzinfo=UTC)


def test_planning_time_does_not_expire_a_room_before_any_question():
    assert chat_deadline(session(), has_started=False) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "voice"},
        {"mode": "video"},
        {"experience_type": "question_practice"},
        {"started_at": None},
        {"started_at": "invalid"},
    ],
)
def test_other_runtimes_and_invalid_start_times_have_no_chat_deadline(changes):
    assert chat_deadline(session(**changes), has_started=True) is None


def test_three_minute_demo_keeps_its_explicit_overtime_without_extending_five_minutes():
    assert chat_deadline(session(duration_minutes=3), has_started=True) == datetime(
        2026, 11, 7, 2, 6, tzinfo=UTC
    )
    assert chat_deadline(session(duration_minutes=25), has_started=True) == datetime(
        2026, 11, 7, 2, 25, tzinfo=UTC
    )


def test_naive_database_timestamp_is_utc_and_does_not_use_machine_timezone():
    assert chat_deadline(session(started_at=datetime(2026, 11, 7, 2)), has_started=True) == (
        datetime(2026, 11, 7, 2, tzinfo=UTC) + timedelta(minutes=5)
    )
