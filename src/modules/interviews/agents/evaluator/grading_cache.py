"""Database-backed store for the grader's validated raw verdicts.

The key hashes the prompt version, model, instructions and the full grading
input (question, expected points, modality, exact answer text), so the same
answer to the same question reuses one verdict and gets the same score. Only
the verdict is stored; the score is recomputed from it on every read, so a
change to the scoring formula applies to cached verdicts too.

Uses the unpooled StandaloneSessionFactory: grading may run on an event loop
other than the API server's. Cache failures never fail grading.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text

from src.infrastructure.database import StandaloneSessionFactory

logger = logging.getLogger(__name__)


class DatabaseVerdictCache:
    async def get(self, key: str) -> dict | None:
        try:
            async with StandaloneSessionFactory() as db:
                stored = await db.scalar(
                    text("SELECT verdict FROM grading_verdict_cache WHERE cache_key = :key"),
                    {"key": key},
                )
        except Exception:
            logger.warning("Grading cache read failed", exc_info=True)
            return None
        if isinstance(stored, str):
            stored = json.loads(stored)
        return stored if isinstance(stored, dict) else None

    async def set(self, key: str, verdict: dict) -> None:
        try:
            async with StandaloneSessionFactory() as db:
                await db.execute(
                    text(
                        """
                        INSERT INTO grading_verdict_cache (cache_key, verdict, created_at)
                        VALUES (:key, CAST(:verdict AS jsonb), now())
                        ON CONFLICT (cache_key) DO NOTHING
                        """
                    ),
                    {"key": key, "verdict": json.dumps(verdict, ensure_ascii=False)},
                )
                await db.commit()
        except Exception:
            logger.warning("Grading cache write failed", exc_info=True)
