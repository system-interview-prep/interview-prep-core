from src.modules.users.repository import UserRepository
from src.modules.users.schemas import ProfilePatch


class UserNotFoundError(Exception):
    pass


def profile_response(row: dict) -> dict:
    created_at = row["created_at"].isoformat()
    avatar = row.get("avatar_url") or row.get("picture")
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "name": row["name"],
        "role": row["role"],
        "provider": row.get("provider", "local"),
        "dob": row["dob"].isoformat() if row.get("dob") else None,
        "picture": avatar,
        "avatar": avatar,
        "credits": {
            "cvScansRemaining": int(row.get("cv_scans_remaining") or 0),
            "mockSessionsRemaining": int(row.get("voice_mock_remaining") or 0),
        },
        "created_at": created_at,
        "createdAt": created_at,
    }


class UserService:
    def __init__(self, repository: UserRepository) -> None:
        self.repository = repository

    async def get_profile(self, user_id: str) -> dict:
        row = await self.repository.get_profile(user_id)
        if row is None:
            raise UserNotFoundError
        return profile_response(row)

    async def update_profile(self, user_id: str, payload: ProfilePatch) -> dict:
        values = payload.model_dump(exclude_unset=True)
        if values:
            await self.repository.update_profile(
                user_id,
                name=values.get("name"),
                dob=values.get("dob"),
            )
            await self.repository.commit()
        return await self.get_profile(user_id)

    async def update_avatar(self, user_id: str, avatar_url: str) -> dict:
        await self.repository.update_avatar(user_id, avatar_url)
        await self.repository.commit()
        return await self.get_profile(user_id)
