"""Close abandoned interview sessions independently of candidate traffic."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.trace_logging import trace_event

logger = logging.getLogger(__name__)

# A candidate who left the room and stays away this long has abandoned the
# interview: voice/video rooms are marked by the agent, chat rooms by the page
# ("left" on close, a heartbeat every minute while open; cleared on return).
CANDIDATE_LEFT_GRACE_MINUTES = 5


async def reap_abandoned_sessions(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Close sessions beyond their budget, preparations abandoned for 2h and rooms the candidate left."""
    async with session_factory() as db:
        result = await db.execute(
            text(
                """
                UPDATE interview_sessions
                SET status = 'CLOSED',
                    end_reason = CASE
                        WHEN started_at IS NULL OR NOT EXISTS (
                            SELECT 1 FROM interview_chat_messages m WHERE m.session_id = interview_sessions.id
                        ) THEN 'TECHNICAL_FAILURE'
                        WHEN metadata->>'candidate_left_at' IS NOT NULL THEN 'CANDIDATE_LEFT'
                        WHEN mode = 'text' AND metadata->>'candidate_seen_at' IS NOT NULL
                             AND CAST(metadata->>'candidate_seen_at' AS TIMESTAMPTZ)
                               < now() - make_interval(mins => :left_grace) THEN 'CANDIDATE_LEFT'
                        ELSE 'HARD_TIMEOUT'
                    END,
                    ended_at = now(),
                    updated_at = now()
                WHERE status = 'OPEN'
                  AND (
                    -- The budget counts from the opening question: started_at is
                    -- the creation time until then (column default), so a session
                    -- still in its lobby must not be closed as "out of time".
                    (started_at IS NOT NULL AND
                     EXISTS (
                        SELECT 1 FROM interview_chat_messages m WHERE m.session_id = interview_sessions.id
                     ) AND
                     started_at + make_interval(mins => duration_minutes) + interval '10 minutes' < now())
                    OR
                    ((started_at IS NULL OR NOT EXISTS (
                        SELECT 1 FROM interview_chat_messages m WHERE m.session_id = interview_sessions.id
                     )) AND updated_at < now() - interval '2 hours')
                    OR
                    (metadata->>'candidate_left_at' IS NOT NULL AND
                     CAST(metadata->>'candidate_left_at' AS TIMESTAMPTZ)
                       < now() - make_interval(mins => :left_grace))
                    OR
                    (mode = 'text' AND metadata->>'candidate_seen_at' IS NOT NULL AND
                     CAST(metadata->>'candidate_seen_at' AS TIMESTAMPTZ)
                       < now() - make_interval(mins => :left_grace))
                  )
                RETURNING id
                """
            ),
            {"left_grace": CANDIDATE_LEFT_GRACE_MINUTES},
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
