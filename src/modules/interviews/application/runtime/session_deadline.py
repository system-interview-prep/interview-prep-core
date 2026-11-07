"""Authoritative wall-clock deadline for timed Chat interviews."""

from datetime import UTC, datetime, timedelta
from typing import Any

from src.modules.interviews.core.interview_design.demo_mode import DEMO_OVERTIME_GRACE_SECONDS, is_demo_duration


def chat_deadline(session: dict[str, Any], *, has_started: bool) -> datetime | None:
    # Practice and media runtimes keep their own lifecycle. Missing fields are
    # legacy Chat rows, matching get_chat_runtime's existing default.
    if not has_started or session.get("mode", "text") != "text":
        return None
    if (session.get("experience_type") or "interview_chat") != "interview_chat":
        return None
    started = session.get("started_at")
    if isinstance(started, str):
        try:
            started = datetime.fromisoformat(started)
        except ValueError:
            return None
    if not isinstance(started, datetime):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    duration = int(session.get("duration_minutes") or 25)
    # Preserve the deliberately turn-driven 3-minute presentation package.
    # A regular 5-minute interview has no overtime allowance.
    grace = DEMO_OVERTIME_GRACE_SECONDS if is_demo_duration(duration) else 0
    return started.astimezone(UTC) + timedelta(minutes=duration, seconds=grace)
