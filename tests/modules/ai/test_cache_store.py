"""Answers of structured model calls are kept once, in files or in the database."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text

from src.core.config import Settings
from src.modules.ai.cache_store import FileCacheStore, SqlCacheStore, _sync_database_url, cache_store_for
from src.modules.ai.structured import CachingStructuredClient


class CountingClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, instructions: str, input_text: str, output_schema: dict) -> str:
        self.calls += 1
        return f'{{"answer": {self.calls}}}'


def _sqlite_store(purpose: str = "judge") -> SqlCacheStore:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE llm_call_cache (cache_key CHAR(64) PRIMARY KEY, purpose VARCHAR(64) NOT NULL, "
                "body TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
        )
    return SqlCacheStore(purpose, engine=engine)


def test_file_store_round_trips_and_misses_cleanly(tmp_path: Path) -> None:
    store = FileCacheStore(tmp_path / "cache")

    assert store.get("k") is None
    store.put("k", '{"a": 1}')
    assert store.get("k") == '{"a": 1}'


def test_sql_store_round_trips_and_keeps_the_first_answer() -> None:
    store = _sqlite_store()

    assert store.get("k") is None
    store.put("k", "first")
    store.put("k", "second")
    assert store.get("k") == "first"


def test_sql_store_failures_are_misses_not_errors() -> None:
    broken = SqlCacheStore("judge", engine=create_engine("sqlite://"))  # no table

    assert broken.get("k") is None
    broken.put("k", "body")  # does not raise


def test_the_caching_client_asks_once_per_distinct_input() -> None:
    inner = CountingClient()
    client = CachingStructuredClient(inner, model="m", prompt_version="v1", store=_sqlite_store())

    first = client.complete(instructions="i", input_text="x", output_schema={})
    again = client.complete(instructions="i", input_text="x", output_schema={})
    other = client.complete(instructions="i", input_text="y", output_schema={})

    assert first == again == '{"answer": 1}'
    assert other == '{"answer": 2}'
    assert inner.calls == 2


def test_a_prompt_change_gets_fresh_keys() -> None:
    store = _sqlite_store()
    inner = CountingClient()
    CachingStructuredClient(inner, model="m", prompt_version="v1", store=store).complete(
        instructions="i", input_text="x", output_schema={}
    )
    CachingStructuredClient(inner, model="m", prompt_version="v2", store=store).complete(
        instructions="i", input_text="x", output_schema={}
    )

    assert inner.calls == 2


def test_the_backend_setting_picks_the_store(tmp_path: Path) -> None:
    assert isinstance(cache_store_for("judge", tmp_path, Settings(llm_cache_backend="file")), FileCacheStore)
    assert isinstance(
        cache_store_for("judge", tmp_path, Settings(llm_cache_backend="postgres")), SqlCacheStore
    )


def test_the_async_url_is_rewritten_for_the_sync_driver() -> None:
    assert _sync_database_url("postgresql+asyncpg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert _sync_database_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert _sync_database_url("sqlite://") == "sqlite://"
