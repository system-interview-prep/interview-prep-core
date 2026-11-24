"""Where a structured model call's answer is kept so the same input is asked once.

Two stores behind one two-method interface.  ``FileCacheStore`` writes one
JSON file per answer under ``models_cache/``, which suits a developer's
machine and the test suite.  ``SqlCacheStore`` keeps the same answers in the
``llm_call_cache`` table, which is what a host without a persistent disk
needs and what lets two instances share one cache.  ``Settings.llm_cache_backend``
picks between them.

Every failure of a store degrades to a miss: a cache must never fail a
match or a parse.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from sqlalchemy import Engine, create_engine, text

from src.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


class CacheStore(Protocol):
    def get(self, key: str) -> str | None: ...

    def put(self, key: str, body: str) -> None: ...


class FileCacheStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def get(self, key: str) -> str | None:
        path = self._directory / f"{key}.json"
        try:
            return path.read_text(encoding="utf-8") if path.is_file() else None
        except Exception:  # noqa: BLE001
            logger.warning("cache file unreadable, recomputing: %s", path.name)
            return None

    def put(self, key: str, body: str) -> None:
        path = self._directory / f"{key}.json"
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        except Exception:  # noqa: BLE001
            logger.warning("cache file not written: %s", path.name)


def _sync_database_url(url: str) -> str:
    """The configured URL on the synchronous psycopg driver.

    The readers that use this cache call the model synchronously, so the
    cache read beside that call is synchronous too; the application's asyncpg
    engine cannot be used from there.
    """
    for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


@lru_cache
def _shared_engine() -> Engine:
    return create_engine(
        _sync_database_url(get_settings().database_url), pool_size=2, max_overflow=2, pool_pre_ping=True
    )


class SqlCacheStore:
    """Answers in the ``llm_call_cache`` table.  ``engine`` is for tests; the app shares one."""

    def __init__(self, purpose: str, *, engine: Engine | None = None) -> None:
        self._purpose = purpose
        self._engine = engine

    def _connect(self) -> Engine:
        return self._engine or _shared_engine()

    def get(self, key: str) -> str | None:
        try:
            with self._connect().connect() as connection:
                row = connection.execute(
                    text("SELECT body FROM llm_call_cache WHERE cache_key = :key"), {"key": key}
                ).first()
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_call_cache unreadable, recomputing: %s", type(exc).__name__)
            return None
        return None if row is None else str(row[0])

    def put(self, key: str, body: str) -> None:
        try:
            with self._connect().begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO llm_call_cache (cache_key, purpose, body) "
                        "VALUES (:key, :purpose, :body) ON CONFLICT (cache_key) DO NOTHING"
                    ),
                    {"key": key, "purpose": self._purpose, "body": body},
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_call_cache not written: %s", type(exc).__name__)


def cache_store_for(purpose: str, directory: Path, settings: Settings | None = None) -> CacheStore:
    """The store the settings ask for; ``directory`` is where the file store would write."""
    settings = settings or get_settings()
    if settings.llm_cache_backend == "postgres":
        return SqlCacheStore(purpose)
    return FileCacheStore(directory)
