import httpx
from sqlalchemy.exc import IntegrityError

from src.core.security import create_access_token, hash_password, verify_password
from src.modules.auth.repository import AuthRepository
from src.modules.auth.schemas import RegisterRequest


class EmailAlreadyExistsError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


class InactiveAccountError(Exception):
    pass


class InvalidGoogleTokenError(Exception):
    pass


def public_user(row: dict) -> dict:
    avatar = row.get("avatar_url") or row.get("picture")
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "name": row["name"],
        "roles": list(row["roles"]),
        "provider": row.get("provider", "local"),
        "picture": avatar,
        "avatar": avatar,
    }


def auth_response(row: dict) -> dict:
    return {
        "access_token": create_access_token(str(row["id"]), row["email"], list(row["roles"])),
        "user": public_user(row),
    }


class AuthService:
    def __init__(self, repository: AuthRepository, google_userinfo_url: str) -> None:
        self.repository = repository
        self.google_userinfo_url = google_userinfo_url

    async def register(self, payload: RegisterRequest) -> dict:
        if await self.repository.get_by_email(payload.email):
            raise EmailAlreadyExistsError
        try:
            user = await self.repository.create_local(
                name=payload.name,
                email=payload.email,
                password_hash=hash_password(payload.password),
                phone=payload.phone,
            )
            await self.repository.commit()
        except IntegrityError as exc:
            await self.repository.rollback()
            raise EmailAlreadyExistsError from exc
        return auth_response(user)

    async def login(self, email: str, password: str) -> dict:
        user = await self.repository.get_by_email(email)
        if not user or not verify_password(password, user.get("password_hash")):
            raise InvalidCredentialsError
        if not user.get("is_active", True):
            raise InactiveAccountError
        return auth_response(user)

    async def google_login(self, token: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    self.google_userinfo_url,
                    headers={"Authorization": f"Bearer {token}"},
                )
            response.raise_for_status()
            profile = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise InvalidGoogleTokenError from exc

        email = str(profile.get("email") or "").strip().lower()
        google_id = str(profile.get("sub") or "").strip()
        if not email or not google_id or profile.get("email_verified") is False:
            raise InvalidGoogleTokenError

        user = await self.repository.get_by_email(email)
        avatar = str(profile.get("picture") or "").strip() or None
        if user is None:
            try:
                user = await self.repository.create_google(
                    email=email,
                    name=str(profile.get("name") or "Google User").strip() or "Google User",
                    google_id=google_id,
                    avatar_url=avatar,
                )
                await self.repository.commit()
            except IntegrityError as exc:
                await self.repository.rollback()
                raise EmailAlreadyExistsError from exc
        else:
            if not user.get("is_active", True):
                raise InactiveAccountError
            await self.repository.link_google_identity(user["id"], google_id=google_id, avatar_url=avatar)
            await self.repository.commit()
            user["google_id"] = user.get("google_id") or google_id
            user["avatar_url"] = avatar or user.get("avatar_url")
        return auth_response(user)
