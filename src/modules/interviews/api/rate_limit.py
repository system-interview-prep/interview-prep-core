"""Small per-process guard for costly interview AI operations."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from fastapi import HTTPException

from src.core.config import get_settings


class InterviewRateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, user_id: str, operation: str) -> None:
        limit = get_settings().interview_ai_requests_per_minute
        key = f"{user_id}:{operation}"
        now = time.monotonic()
        async with self._lock:
            events = self._events[key]
            while events and now - events[0] >= 60.0:
                events.popleft()
            if len(events) >= limit:
                raise HTTPException(
                    status_code=429,
                    detail="Bạn thao tác quá nhanh. Vui lòng chờ một phút rồi thử lại.",
                    headers={"Retry-After": "60"},
                )
            events.append(now)


interview_rate_limiter = InterviewRateLimiter()
