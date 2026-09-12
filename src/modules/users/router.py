import base64

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db
from src.modules.users.repository import UserRepository
from src.modules.users.schemas import ProfilePatch, ProfileResponse
from src.modules.users.service import UserNotFoundError, UserService, profile_response

router = APIRouter(tags=["users"])

ALLOWED_AVATAR_MIMES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/gif",
    "image/webp",
}
MAX_AVATAR_BYTES = 5 * 1024 * 1024


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


@router.post("/users/me/picture", response_model=ProfileResponse)
@router.post("/user/profile/picture", response_model=ProfileResponse)
async def upload_profile_picture(
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    content_type = (file.content_type or "").lower()
    if content_type not in ALLOWED_AVATAR_MIMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Chỉ hỗ trợ ảnh định dạng JPG, PNG, GIF hoặc WebP.",
        )

    content = await file.read(MAX_AVATAR_BYTES + 1)
    if len(content) > MAX_AVATAR_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Kích thước ảnh vượt quá giới hạn 5MB.",
        )

    encoded = base64.b64encode(content).decode("ascii")
    avatar_url = f"data:{content_type};base64,{encoded}"

    try:
        return await _service(db).update_avatar(user["sub"], avatar_url)
    except UserNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy người dùng."
        ) from exc
