"""Close abandoned interview sessions independently of candidate traffic."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.trace_logging import trace_event

logger = logging.getLogger(__name__)


async def reap_abandoned_sessions(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Close sessions beyond their budget and preparations abandoned for 2h."""
    async with session_factory() as db:
        result = await db.execute(
            text(
                """
                UPDATE interview_sessions
                SET status = 'CLOSED',
                    end_reason = CASE
                        WHEN started_at IS NULL THEN 'TECHNICAL_FAILURE'
                        ELSE 'HARD_TIMEOUT'
                    END,
                    ended_at = now(),
                    updated_at = now()
                WHERE status = 'OPEN'
                  AND (
                    (started_at IS NOT NULL AND
                     started_at + make_interval(mins => duration_minutes) + interval '10 minutes' < now())
                    OR
                    (started_at IS NULL AND updated_at < now() - interval '2 hours')
                  )
                RETURNING id
                """
            )
        )
        closed = [str(row[0]) for row in result.all()]
        await db.commit()
    if closed:
        trace_event("interviewer", "abandoned_sessions_reaped", count=len(closed))
    return len(closed)


async def session_reaper_loop(
    session_factory: async_sessionmaker[AsyncSession], *, interval_seconds: float = 60.0
) -> None:
    while True:
        try:
            await reap_abandoned_sessions(session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Interview session reaper failed")
        await asyncio.sleep(interval_seconds)
