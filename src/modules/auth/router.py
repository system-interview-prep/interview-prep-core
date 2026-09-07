from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.security import create_access_token, hash_password, verify_password
from src.infrastructure.database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str | None = Field(default=None, min_length=6)
    name: str = "User"
    dob: str | None = None
    role: str = "CANDIDATE"
    provider: str = "local"


class LoginRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str | None = None


class GoogleLoginRequest(BaseModel):
    accessToken: str = Field(min_length=1)


def _public_user(row: dict) -> dict:
    return {key: row[key] for key in ("id", "email", "name", "role", "provider")}


@router.post("/register")
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)) -> dict:
    email = payload.email.lower()
    existing = await db.execute(text("SELECT id FROM users WHERE lower(email) = :email"), {"email": email})
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email is already registered")
    if payload.provider == "local" and not payload.password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password is required")
    user_id = str(uuid4())
    await db.execute(
        text("""INSERT INTO users (id, email, password, name, role, provider, dob)
        VALUES (:id, :email, :password, :name, :role, :provider, CAST(:dob AS date))"""),
        {
            "id": user_id,
            "email": email,
            "password": hash_password(payload.password) if payload.password else None,
            "name": payload.name.strip() or "User",
            "role": payload.role,
            "provider": payload.provider,
            "dob": payload.dob,
        },
    )
    await db.commit()
    result = {"message": "User registered successfully"}
    if payload.provider == "google":
        result["access_token"] = create_access_token(user_id, email, payload.role)
    return result


@router.post("/login")
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(
        text("SELECT id, email, password, name, role, provider FROM users WHERE lower(email) = :email"),
        {"email": payload.email.lower()},
    )
    row = result.mappings().one_or_none()
    if not row or (
        row["provider"] == "local" and not verify_password(payload.password or "", row["password"])
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return {
        "access_token": create_access_token(row["id"], row["email"], row["role"]),
        "user": _public_user(row),
    }


@router.post("/google")
async def google_login(payload: GoogleLoginRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """Validate Google's access token and issue this application's JWT."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                get_settings().google_oauth_userinfo_url,
                headers={"Authorization": f"Bearer {payload.accessToken}"},
            )
        response.raise_for_status()
        google_profile = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google token") from exc

    email = str(google_profile.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google account does not have an email",
        )
    if google_profile.get("email_verified") is False:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Google email is not verified")

    existing = await db.execute(
        text("SELECT id, email, name, role, provider, picture FROM users WHERE lower(email) = :email"),
        {"email": email},
    )
    row = existing.mappings().one_or_none()
    if row is None:
        user_id = str(uuid4())
        name = str(google_profile.get("name") or "Google User").strip() or "Google User"
        picture = google_profile.get("picture")
        await db.execute(
            text("""INSERT INTO users (id, email, name, role, provider, picture)
            VALUES (:id, :email, :name, 'CANDIDATE', 'google', :picture)"""),
            {"id": user_id, "email": email, "name": name, "picture": picture},
        )
        await db.commit()
        row = {
            "id": user_id, "email": email, "name": name, "role": "CANDIDATE",
            "provider": "google", "picture": picture,
        }
    return {
        "access_token": create_access_token(row["id"], row["email"], row["role"]),
        "user": _public_user(row),
    }
