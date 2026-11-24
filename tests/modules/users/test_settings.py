from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.modules.users.settings_schemas import CandidateSettingsPayload
from src.modules.users.settings_service import CandidateSettingsService, settings_response


def settings_row(**overrides: object) -> dict:
    return {
        "ai_voice": "vi-north-male",
        "speech_rate": Decimal("1.0"),
        "auto_camera_webrtc": True,
        "ai_tone": "standard",
        "notify_weekly_reminder": True,
        "notify_job_match": True,
        "notify_progress_report": True,
        "notify_sound_fx": True,
        "updated_at": datetime.now(UTC),
        **overrides,
    }


class SettingsRepository:
    def __init__(self, row: dict | None = None) -> None:
        self.row = row
        self.upserted: tuple[str, CandidateSettingsPayload] | None = None

    async def get(self, user_id: str) -> dict | None:
        return self.row if user_id == "candidate-1" else None

    async def upsert(self, user_id: str, payload: CandidateSettingsPayload) -> dict:
        self.upserted = (user_id, payload)
        self.row = settings_row(
            ai_voice=payload.aiVoice,
            speech_rate=Decimal(str(payload.speechRate)),
            auto_camera_webrtc=payload.autoCameraWebRTC,
            ai_tone=payload.aiTone,
            notify_weekly_reminder=payload.notifyWeeklyReminder,
            notify_job_match=payload.notifyJobMatch,
            notify_progress_report=payload.notifyProgressReport,
            notify_sound_fx=payload.notifySoundFx,
        )
        return self.row


def test_settings_payload_rejects_values_outside_supported_options() -> None:
    with pytest.raises(ValidationError):
        CandidateSettingsPayload(
            aiVoice="voice",
            speechRate=1.5,
            autoCameraWebRTC=True,
            aiTone="standard",
            notifyWeeklyReminder=True,
            notifyJobMatch=True,
            notifyProgressReport=True,
            notifySoundFx=True,
        )

    with pytest.raises(ValidationError):
        CandidateSettingsPayload(
            aiVoice="voice",
            speechRate=1.0,
            autoCameraWebRTC=True,
            aiTone="casual",
            notifyWeeklyReminder=True,
            notifyJobMatch=True,
            notifyProgressReport=True,
            notifySoundFx=True,
        )


def test_settings_mapper_returns_frontend_contract() -> None:
    row = settings_row(speech_rate=Decimal("1.2"), ai_tone="supportive")

    assert settings_response(row) == {
        "aiVoice": "vi-north-male",
        "speechRate": 1.2,
        "autoCameraWebRTC": True,
        "aiTone": "supportive",
        "notifyWeeklyReminder": True,
        "notifyJobMatch": True,
        "notifyProgressReport": True,
        "notifySoundFx": True,
        "updatedAt": row["updated_at"],
    }


@pytest.mark.asyncio
async def test_get_settings_returns_existing_user_preferences() -> None:
    repository = SettingsRepository(settings_row(ai_voice="en-us-natural"))
    service = CandidateSettingsService(repository)

    result = await service.get("candidate-1")

    assert result["aiVoice"] == "en-us-natural"
    assert repository.upserted is None


@pytest.mark.asyncio
async def test_get_settings_creates_defaults_for_user_without_a_row() -> None:
    repository = SettingsRepository()
    service = CandidateSettingsService(repository)

    result = await service.get("candidate-1")

    assert result["aiVoice"] == "vi-north-male"
    assert result["speechRate"] == 1.0
    assert repository.upserted is not None
    assert repository.upserted[0] == "candidate-1"


@pytest.mark.asyncio
async def test_update_settings_persists_all_candidate_fields() -> None:
    repository = SettingsRepository()
    service = CandidateSettingsService(repository)
    payload = CandidateSettingsPayload(
        aiVoice="en-us-natural",
        speechRate=1.2,
        autoCameraWebRTC=False,
        aiTone="supportive",
        notifyWeeklyReminder=False,
        notifyJobMatch=True,
        notifyProgressReport=False,
        notifySoundFx=False,
    )

    result = await service.update("candidate-1", payload)

    assert repository.upserted == ("candidate-1", payload)
    assert result["aiVoice"] == "en-us-natural"
    assert result["speechRate"] == 1.2
    assert result["autoCameraWebRTC"] is False
    assert result["aiTone"] == "supportive"
    assert result["notifyWeeklyReminder"] is False
    assert result["notifyJobMatch"] is True
    assert result["notifyProgressReport"] is False
    assert result["notifySoundFx"] is False
