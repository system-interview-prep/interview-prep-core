from datetime import UTC, datetime, timedelta
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.core.config import get_settings

bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, password_hash: str | None) -> bool:
    return bool(password_hash) and bcrypt.checkpw(password.encode(), password_hash.encode())


def create_access_token(user_id: str, email: str, roles: list[str]) -> str:
    settings = get_settings()
    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=settings.jwt_expires_minutes)
    roles = sorted(set(roles))
    if not roles:
        raise ValueError("An access token requires at least one role.")
    return jwt.encode(
        {
            "sub": user_id,
            "userId": user_id,
            "email": email,
            "roles": roles,
            "iat": issued_at,
            "exp": expires_at,
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def require_roles(*allowed_roles: str):
    """Permit selected actors; ADMIN is always an emergency override."""

    async def guard(user: Annotated[dict[str, str], Depends(current_user)]) -> dict[str, str]:
        if not set(user.get("roles", [])).intersection({*allowed_roles, "ADMIN"}):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient actor role.")
        return user

    return guard


async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> dict[str, str]:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Chưa xác thực.")
    settings = get_settings()
    try:
        payload = jwt.decode(
            credentials.credentials, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        return {"sub": str(payload["sub"]), "email": str(payload["email"]), "roles": [str(role) for role in payload["roles"]]}
    except (jwt.InvalidTokenError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token không hợp lệ hoặc đã hết hạn.",
        ) from exc


async def require_admin(user: Annotated[dict[str, str], Depends(current_user)]) -> dict[str, str]:
    if "ADMIN" not in user.get("roles", []):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Yêu cầu quyền quản trị viên.")
    return user
