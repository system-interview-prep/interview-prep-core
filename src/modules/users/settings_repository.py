from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.users.settings_schemas import CandidateSettingsPayload


class CandidateSettingsRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, user_id: str) -> dict | None:
        result = await self.db.execute(
            text(
                "SELECT ai_voice, speech_rate, auto_camera_webrtc, ai_tone, "
                "notify_weekly_reminder, notify_job_match, notify_progress_report, "
                "notify_sound_fx, updated_at FROM candidate_settings WHERE user_id = :user_id"
            ),
            {"user_id": user_id},
        )
        row = result.mappings().one_or_none()
        return dict(row) if row else None

    async def upsert(self, user_id: str, payload: CandidateSettingsPayload) -> dict:
        values = {
            "ai_voice": payload.aiVoice,
            "speech_rate": payload.speechRate,
            "auto_camera_webrtc": payload.autoCameraWebRTC,
            "ai_tone": payload.aiTone,
            "notify_weekly_reminder": payload.notifyWeeklyReminder,
            "notify_job_match": payload.notifyJobMatch,
            "notify_progress_report": payload.notifyProgressReport,
            "notify_sound_fx": payload.notifySoundFx,
        }
        result = await self.db.execute(
            text(
                "INSERT INTO candidate_settings (user_id, ai_voice, speech_rate, auto_camera_webrtc, "
                "ai_tone, notify_weekly_reminder, notify_job_match, notify_progress_report, notify_sound_fx) "
                "VALUES (:user_id, :ai_voice, :speech_rate, :auto_camera_webrtc, :ai_tone, "
                ":notify_weekly_reminder, :notify_job_match, :notify_progress_report, :notify_sound_fx) "
                "ON CONFLICT (user_id) DO UPDATE SET ai_voice = EXCLUDED.ai_voice, "
                "speech_rate = EXCLUDED.speech_rate, auto_camera_webrtc = EXCLUDED.auto_camera_webrtc, "
                "ai_tone = EXCLUDED.ai_tone, notify_weekly_reminder = EXCLUDED.notify_weekly_reminder, "
                "notify_job_match = EXCLUDED.notify_job_match, "
                "notify_progress_report = EXCLUDED.notify_progress_report, "
                "notify_sound_fx = EXCLUDED.notify_sound_fx, updated_at = now() "
                "RETURNING ai_voice, speech_rate, auto_camera_webrtc, ai_tone, "
                "notify_weekly_reminder, notify_job_match, notify_progress_report, "
                "notify_sound_fx, updated_at"
            ),
            {"user_id": user_id, **values},
        )
        row = result.mappings().one()
        await self.db.commit()
        return dict(row)
