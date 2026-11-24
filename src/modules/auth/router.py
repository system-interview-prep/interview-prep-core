from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.infrastructure.database import get_db
from src.modules.auth.rate_limit import login_rate_limiter
from src.modules.auth.repository import AuthRepository
from src.modules.auth.schemas import AuthResponse, GoogleLoginRequest, LoginRequest, RegisterRequest
from src.modules.auth.service import (
    AuthService,
    EmailAlreadyExistsError,
    InactiveAccountError,
    InvalidCredentialsError,
    InvalidGoogleTokenError,
    public_user,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _public_user(row: dict) -> dict:
    """Backward-compatible public mapper; sensitive columns are always excluded."""
    return public_user(row)


def _service(db: AsyncSession) -> AuthService:
    return AuthService(AuthRepository(db), get_settings().google_oauth_userinfo_url)


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        return await _service(db).register(payload)
    except EmailAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email đã được đăng ký.") from exc


@router.post("/login", response_model=AuthResponse)
async def login(payload: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    client_ip = request.client.host if request.client else "unknown"
    if login_rate_limiter.is_blocked(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Bạn đã đăng nhập sai quá nhiều lần. Vui lòng thử lại sau 1 phút.",
        )
    try:
        response = await _service(db).login(payload.email, payload.password)
    except InvalidCredentialsError as exc:
        login_rate_limiter.record_failure(client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email hoặc mật khẩu không chính xác.",
        ) from exc
    except InactiveAccountError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tài khoản đã bị vô hiệu hóa."
        ) from exc
    login_rate_limiter.reset(client_ip)
    return response


@router.post("/google", response_model=AuthResponse)
@router.post("/google-login", response_model=AuthResponse, include_in_schema=False)
async def google_login(payload: GoogleLoginRequest, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        return await _service(db).google_login(payload.token)
    except InvalidGoogleTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Google token không hợp lệ."
        ) from exc
    except InactiveAccountError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tài khoản đã bị vô hiệu hóa."
        ) from exc
    except EmailAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email đã được đăng ký.") from exc
