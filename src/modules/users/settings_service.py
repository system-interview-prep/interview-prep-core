from src.modules.users.settings_repository import CandidateSettingsRepository
from src.modules.users.settings_schemas import CandidateSettingsPayload


def settings_response(row: dict) -> dict:
    return {
        "aiVoice": row["ai_voice"],
        "speechRate": float(row["speech_rate"]),
        "autoCameraWebRTC": row["auto_camera_webrtc"],
        "aiTone": row["ai_tone"],
        "notifyWeeklyReminder": row["notify_weekly_reminder"],
        "notifyJobMatch": row["notify_job_match"],
        "notifyProgressReport": row["notify_progress_report"],
        "notifySoundFx": row["notify_sound_fx"],
        "updatedAt": row["updated_at"],
    }


class CandidateSettingsService:
    def __init__(self, repository: CandidateSettingsRepository) -> None:
        self.repository = repository

    async def get(self, user_id: str) -> dict:
        row = await self.repository.get(user_id)
        if row is None:
            # Supports users created after the migration and safely handles concurrent requests.
            row = await self.repository.upsert(
                user_id,
                CandidateSettingsPayload(
                    aiVoice="vi-north-male",
                    speechRate=1.0,
                    autoCameraWebRTC=True,
                    aiTone="standard",
                    notifyWeeklyReminder=True,
                    notifyJobMatch=True,
                    notifyProgressReport=True,
                    notifySoundFx=True,
                ),
            )
        return settings_response(row)

    async def update(self, user_id: str, payload: CandidateSettingsPayload) -> dict:
        return settings_response(await self.repository.upsert(user_id, payload))
