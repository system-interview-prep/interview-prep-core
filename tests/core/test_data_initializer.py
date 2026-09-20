from unittest.mock import AsyncMock

import pytest

from src.core.config import Settings
from src.core.data_initializer import initialize_data


class _Result:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> str | None:
        return self.value


class _SessionContext:
    def __init__(self, session: AsyncMock) -> None:
        self.session = session

    async def __aenter__(self) -> AsyncMock:
        return self.session

    async def __aexit__(self, *_: object) -> None:
        return None


def _factory(session: AsyncMock):
    return lambda: _SessionContext(session)


@pytest.mark.asyncio
async def test_initializer_skips_when_bootstrap_credentials_are_not_configured() -> None:
    session = AsyncMock()
    created = await initialize_data(_factory(session), Settings())
    assert created is False
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_initializer_creates_admin_and_role_assignment() -> None:
    session = AsyncMock()
    session.execute.side_effect = [_Result(None), _Result(None), _Result(None), _Result(None)]
    settings = Settings(bootstrap_admin_email="admin@intervia.io", bootstrap_admin_password="Admin@123456")

    created = await initialize_data(_factory(session), settings)

    assert created is True
    assert session.execute.await_count == 4
    assert session.commit.await_count == 1
    statements = [call.args[0].text for call in session.execute.await_args_list]
    assert any("INSERT INTO users" in statement for statement in statements)
    assert any("user_role_assignments" in statement and "'ADMIN'" in statement for statement in statements)


@pytest.mark.asyncio
async def test_initializer_does_not_reset_existing_account_password() -> None:
    session = AsyncMock()
    session.execute.side_effect = [_Result("existing-user"), _Result(None), _Result(None)]
    settings = Settings(bootstrap_admin_email="admin@intervia.io", bootstrap_admin_password="Admin@123456")

    created = await initialize_data(_factory(session), settings)

    assert created is False
    statements = [call.args[0].text for call in session.execute.await_args_list]
    assert not any("INSERT INTO users" in statement for statement in statements)
    assert any("user_role_assignments" in statement for statement in statements)
