from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CandidateSettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aiVoice: str = Field(min_length=1, max_length=100)
    speechRate: Literal[0.8, 1.0, 1.2]
    autoCameraWebRTC: bool
    aiTone: Literal["supportive", "standard", "strict"]
    notifyWeeklyReminder: bool
    notifyJobMatch: bool
    notifyProgressReport: bool
    notifySoundFx: bool


class CandidateSettingsResponse(CandidateSettingsPayload):
    updatedAt: datetime
