from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from src.modules.interviews.api.rate_limit import InterviewRateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_rejects_requests_over_the_per_operation_limit() -> None:
    limiter = InterviewRateLimiter()
    settings = SimpleNamespace(interview_ai_requests_per_minute=2)

    with (
        patch("src.modules.interviews.api.rate_limit.get_settings", return_value=settings),
        patch("src.modules.interviews.api.rate_limit.time.monotonic", side_effect=[0.0, 1.0, 2.0]),
    ):
        await limiter.check("user-1", "respond")
        await limiter.check("user-1", "respond")
        with pytest.raises(HTTPException) as exc_info:
            await limiter.check("user-1", "respond")

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers == {"Retry-After": "60"}


@pytest.mark.asyncio
async def test_rate_limiter_expires_old_events() -> None:
    limiter = InterviewRateLimiter()
    settings = SimpleNamespace(interview_ai_requests_per_minute=1)

    with (
        patch("src.modules.interviews.api.rate_limit.get_settings", return_value=settings),
        patch("src.modules.interviews.api.rate_limit.time.monotonic", side_effect=[0.0, 60.0]),
    ):
        await limiter.check("user-1", "evaluate")
        await limiter.check("user-1", "evaluate")
