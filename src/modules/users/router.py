from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db
from src.modules.users.repository import UserRepository
from src.modules.users.schemas import ProfilePatch, ProfileResponse
from src.modules.users.service import UserNotFoundError, UserService, profile_response

router = APIRouter(tags=["users"])


def _profile(row: dict) -> dict:
    """Backward-compatible mapper used by existing callers and tests."""
    return profile_response(row)


def _service(db: AsyncSession) -> UserService:
    return UserService(UserRepository(db))


async def _get_profile(user: dict, db: AsyncSession) -> dict:
    try:
        return await _service(db).get_profile(user["sub"])
    except UserNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy người dùng."
        ) from exc


@router.get("/users/me", response_model=ProfileResponse)
@router.get("/user/profile", response_model=ProfileResponse)
async def get_profile(
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _get_profile(user, db)


@router.patch("/users/me", response_model=ProfileResponse)
@router.patch("/user/profile", response_model=ProfileResponse)
async def update_profile(
    payload: ProfilePatch,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await _service(db).update_profile(user["sub"], payload)
    except UserNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy người dùng."
        ) from exc
