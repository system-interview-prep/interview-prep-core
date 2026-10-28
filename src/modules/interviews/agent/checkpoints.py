"""Production checkpoint lifecycle. PostgreSQL business tables remain authoritative."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from src.core.config import get_settings

_checkpointer: Any = None


def get_interview_checkpointer() -> Any:
    return _checkpointer


@asynccontextmanager
async def interview_checkpoint_lifespan() -> AsyncIterator[None]:
    global _checkpointer
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool
    except ImportError as exc:
        raise RuntimeError("Install langgraph-checkpoint-postgres for interview checkpoints") from exc

    database_url = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    serializer = JsonPlusSerializer(allowed_msgpack_modules=None)
    async with AsyncConnectionPool(
        database_url,
        min_size=1,
        max_size=10,
        open=False,
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
    ) as pool:
        saver = AsyncPostgresSaver(pool, serde=serializer)
        await saver.setup()
        _checkpointer = saver
        try:
            yield
        finally:
            _checkpointer = None
