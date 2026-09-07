import pytest
from fastapi import HTTPException

from src.core.security import require_admin


@pytest.mark.asyncio
async def test_require_admin_accepts_only_admin() -> None:
    assert (await require_admin({"sub": "u1", "role": "ADMIN"}))["sub"] == "u1"
    with pytest.raises(HTTPException) as error:
        await require_admin({"sub": "u1", "role": "CANDIDATE"})
    assert error.value.status_code == 403
