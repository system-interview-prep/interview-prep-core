"""Admin-only room issuance; worker events use a separate, short-lived scope."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import uuid4

import jwt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.core.config import get_settings
from src.core.security import bearer_scheme, require_admin
from src.core.trace_logging import trace_event
from src.modules.interviews.adapters.voice.stt_provider import stt_provider_settings

from .stt_test import SttTestOptions

router = APIRouter()
AUDIENCE = "admin-stt-test"
SCOPE = "admin:stt-test-runtime"


def trace_enabled(settings) -> bool:
    return (
        settings.trace_logs_enabled
        if settings.trace_logs_enabled is not None
        else settings.app_env.casefold() in {"development", "local"}
    )


async def runtime_claims(credentials=Depends(bearer_scheme)) -> dict:
    settings = get_settings()
    try:
        claims = jwt.decode(
            credentials.credentials if credentials else "",
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=AUDIENCE,
            options={"require": ["exp", "iat", "sub", "testId", "roomName", "identity", "scope"]},
        )
        if claims.get("scope") != SCOPE or claims.get("tokenType") != "stt-test-runtime":
            raise ValueError("wrong scope")
        return claims
    except (jwt.PyJWTError, ValueError) as exc:
        raise HTTPException(401, "Invalid STT test runtime credential") from exc


@router.post("/ai/speech/test/livekit-token")
async def issue_token(payload: SttTestOptions, admin: Annotated[dict, Depends(require_admin)]) -> dict:
    settings = get_settings()
    if not all((settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret)):
        raise HTTPException(503, "LiveKit is not configured")
    from livekit import api

    now = datetime.now(UTC)
    test_id = str(uuid4())
    room_name = f"stt-test-{test_id}"
    identity = f"stt-admin-{admin['sub']}"
    runtime = jwt.encode(
        {
            "sub": str(admin["sub"]),
            "testId": test_id,
            "roomName": room_name,
            "identity": identity,
            "scope": SCOPE,
            "tokenType": "stt-test-runtime",
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + timedelta(minutes=25),
            "context": payload.context,
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    token = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name("Admin STT test")
        .with_ttl(timedelta(minutes=5))
        .with_grants(
            api.VideoGrants(
                room_join=True, room=room_name, can_publish=True, can_subscribe=True, can_publish_data=True
            )
        )
        .with_room_config(
            api.RoomConfiguration(
                agents=[
                    api.RoomAgentDispatch(
                        agent_name=settings.livekit_agent_name,
                        metadata=f"stt-test:{runtime}",
                    )
                ]
            )
        )
        .to_jwt()
    )
    trace_event(
        "test_stt",
        "stt_test_issued",
        test_id=test_id,
        room_name=room_name,
        context=payload.context,
        reference_text=payload.referenceText,
        mode="livekit",
    )
    return {
        "serverUrl": settings.livekit_url,
        "participantToken": token,
        "roomName": room_name,
        "testId": test_id,
        "traceEnabled": trace_enabled(settings),
    }


@router.post("/ai/speech/test/livekit/runtime/start")
async def runtime_start(claims: Annotated[dict, Depends(runtime_claims)]) -> dict:
    provider = stt_provider_settings()
    return {
        "testId": claims["testId"],
        "roomName": claims["roomName"],
        "identity": claims["identity"],
        "context": claims.get("context", ""),
        "locale": "vi-VN",
        "provider": provider.provider,
        "model": provider.model,
        "traceEnabled": trace_enabled(get_settings()),
    }


class WorkerEvent(BaseModel):
    event: str = Field(pattern=r"^stt_test_[a-z_]+$", max_length=80)
    fields: dict[str, Any] = Field(default_factory=dict, max_length=40)


@router.post("/ai/speech/test/livekit/runtime/events")
async def runtime_event(payload: WorkerEvent, claims: Annotated[dict, Depends(runtime_claims)]) -> dict:
    import json

    if len(json.dumps(payload.fields)) > 100_000:
        raise HTTPException(413, "STT diagnostic event too large")
    fields = {
        key: value
        for key, value in payload.fields.items()
        if key not in {"test_id", "event", "workflow", "timestamp", "category", "session_id", "room_name"}
    }
    trace_event("test_stt", payload.event, test_id=claims["testId"], room_name=claims["roomName"], **fields)
    return {"accepted": True}
