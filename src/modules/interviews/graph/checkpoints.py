"""Production checkpoint lifecycle. PostgreSQL business tables remain authoritative."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from src.core.config import get_settings

_checkpointer: Any = None
_store: Any = None


def get_interview_checkpointer() -> Any:
    return _checkpointer


def get_interview_store() -> Any:
    """The LangGraph store for candidate memory, or None (memory off / outside the lifespan)."""
    return _store


def _psycopg_url() -> str:
    database_url = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    return database_url


@asynccontextmanager
async def interview_memory_store() -> AsyncIterator[Any]:
    """The candidate-memory store for one background job.

    Inside the app lifespan this is the shared store. A Celery worker has no
    lifespan, so it opens a small pool of its own for the job and closes it
    afterwards. A store that cannot be opened yields ``None`` -- memory off --
    exactly as the lifespan treats it; the job then reports ``store_unavailable``.
    """
    if _store is not None:
        yield _store
        return
    try:
        from langgraph.store.postgres.aio import AsyncPostgresStore
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool
    except ImportError as exc:
        logging.getLogger(__name__).warning("candidate memory store unavailable: %s", exc)
        yield None
        return
    async with AsyncConnectionPool(
        _psycopg_url(),
        min_size=1,
        max_size=2,
        open=False,
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
    ) as pool:
        store = AsyncPostgresStore(pool)
        await store.setup()
        yield store


@asynccontextmanager
async def interview_checkpoint_lifespan() -> AsyncIterator[None]:
    global _checkpointer, _store
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool
    except ImportError as exc:
        raise RuntimeError("Install langgraph-checkpoint-postgres for interview checkpoints") from exc

    serializer = JsonPlusSerializer(allowed_msgpack_modules=None)
    async with AsyncConnectionPool(
        _psycopg_url(),
        min_size=1,
        max_size=10,
        open=False,
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
    ) as pool:
        saver = AsyncPostgresSaver(pool, serde=serializer)
        await saver.setup()
        _checkpointer = saver
        # Candidate memory (INTERVIEW_AGENT_DECISION_PLAN_V1 §7) shares the pool. A store
        # that cannot be set up only switches memory off; the interview itself runs.
        try:
            from langgraph.store.postgres.aio import AsyncPostgresStore

            store = AsyncPostgresStore(pool)
            await store.setup()
            _store = store
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("candidate memory store unavailable: %s", exc)
            _store = None
        try:
            yield
        finally:
            _checkpointer = None
            _store = None
