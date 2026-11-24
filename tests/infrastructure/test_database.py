import pytest

from src.infrastructure.database import _async_database_url


def test_async_database_url_conversion() -> None:
    assert _async_database_url("postgresql://u:p@db/x") == "postgresql+asyncpg://u:p@db/x"
    assert _async_database_url("postgres://u:p@db/x") == "postgresql+asyncpg://u:p@db/x"
    with pytest.raises(ValueError):
        _async_database_url("sqlite:///x.db")
