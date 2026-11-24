from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import get_settings


def _async_database_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    raise ValueError("DATABASE_URL must use the postgresql scheme")


engine: AsyncEngine = create_async_engine(
    _async_database_url(get_settings().database_url),
    pool_pre_ping=True,
)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)

# For code that runs its own event loop (asyncio.run inside a threadpool
# thread). The pooled engine above hands out asyncpg connections bound to the
# server's loop; reusing one from another loop corrupts it ("attached to a
# different loop", "connection was closed in the middle of operation") and took
# the API worker down. Unpooled: every session opens and closes its own
# connection on the calling loop.
StandaloneSessionFactory = async_sessionmaker(
    create_async_engine(_async_database_url(get_settings().database_url), poolclass=NullPool),
    expire_on_commit=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session


@asynccontextmanager
async def postgres_lifespan() -> AsyncIterator[None]:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    try:
        yield
    finally:
        await engine.dispose()
