import io
import os
import wave
from time import perf_counter
from typing import Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import require_admin
from src.infrastructure.database import get_db
from src.modules.admin.ai import ai_overview, langfuse_view
from src.modules.admin.ai.service import enrich_flagged_turns
from src.modules.admin.ai.service import get_ai_call as get_ai_call_view
from src.modules.admin.ai.service import list_ai_calls as list_ai_calls_view
from src.modules.admin.shared.audit import log_admin_action as _log_admin_action
from src.modules.interviews.facade import (
    interview_ai_alerts,
    interview_ai_health,
    interview_langfuse_links,
    interview_speech_control,
    interviewer_playbook,
    review_interview_speech_check,
)

from .schemas import (
    AdminSpeechTestRequest,
    PlaybookDecisionRequest,
    PlaybookEntryRequest,
    PlaybookReflectRequest,
    SpeechCheckReview,
    TraceReviewRequest,
)
from .stt_livekit import router as stt_livekit_router

router = APIRouter(tags=["admin"])
router.include_router(stt_livekit_router)


@router.get("/ai/alerts")
async def get_admin_ai_alerts(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Problems to act on now: failed or stuck grading, live errors, high fallback rates."""
    return {"success": True, **await interview_ai_alerts(db)}

@router.get("/ai/health")
async def get_admin_ai_health(
    days: int = Query(7, ge=1, le=90),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Fallback rates, latencies, failures and stuck sessions over a window."""
    try:
        health = await interview_ai_health(db, days=days)
    except Exception as exc:  # tables missing until migration 0029 is applied
        await db.rollback()
        raise HTTPException(status_code=503, detail="AI observability tables are not migrated") from exc
    return {"success": True, **health, "langfuse": await interview_langfuse_links()}

@router.get("/ai/models")
async def get_admin_ai_models(
    days: int = Query(30, ge=1, le=90),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Provider and model of each AI feature: configured, and seen in ai_call_logs."""
    return {"success": True, **await ai_overview.models_view(db, days=days)}

@router.get("/langfuse/traces/{trace_id}")
async def get_langfuse_trace(
    trace_id: str,
    session_id: str | None = Query(None, alias="sessionId", max_length=36),
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One turn's model calls and scores, read back from Langfuse."""
    view = await langfuse_view.trace_view(trace_id[:64])
    # Sampled turns carry the candidate's (masked) answer: reading it is logged
    # like "Xem c?u tr? l?i".
    if view.get("connected") and any(o.get("input") for o in view.get("observations") or []):
        await _log_admin_action(db, admin, "VIEW_AI_TRACE", session_id=session_id, target_id=trace_id[:64])
    return {"success": True, **view}


@router.post("/langfuse/traces/{trace_id}/reviews", status_code=201)
async def review_langfuse_trace(
    trace_id: str,
    payload: TraceReviewRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A reviewer's verdict on a turn, written to Langfuse as a ``human.*`` score."""
    try:
        await langfuse_view.record_review(trace_id[:64], payload.name, payload.value, payload.comment)
    except langfuse_view.ReviewScoreError as exc:
        raise HTTPException(status_code=422 if str(exc) == "unknown_score" else 503, detail=str(exc)) from exc
    await _log_admin_action(
        db,
        admin,
        "REVIEW_AI_TURN",
        session_id=payload.sessionId,
        target_id=trace_id[:64],
        detail={"score": payload.name, "value": payload.value, "note": payload.comment},
    )
    return {"success": True}

@router.get("/sessions/{session_id}/langfuse")
async def get_session_langfuse(session_id: str, _: dict = Depends(require_admin)) -> dict:
    """Every Langfuse trace of one interview (turns and grading) with totals and session scores."""
    return {"success": True, **await langfuse_view.session_view(session_id[:36])}

@router.get("/langfuse/flagged")
async def get_flagged_turns(
    days: int = Query(7, ge=1, le=90),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The latest turns a rule flagged, with the interview and turn they belong to."""
    view = await enrich_flagged_turns(db, await langfuse_view.flagged_turns(days=days))
    return {"success": True, **view}




def _playbook_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=getattr(exc, "status_code", 400), detail=getattr(exc, "code", str(exc)))

@router.get("/interviewer-playbook")
async def get_interviewer_playbook(
    admin: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    """Lessons from past interviews: proposed, active (in the prompts), retired; the last reflection."""
    return {"success": True, **await interviewer_playbook.view(db, actor_id=str(admin.get("sub") or ""))}

@router.post("/interviewer-playbook/reflect", status_code=202)
async def reflect_interviewer_playbook(
    payload: PlaybookReflectRequest | None = None,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Propose lessons from recent flagged and reviewed turns (one LLM call, background)."""
    days = payload.days if payload else 14
    try:
        run = interviewer_playbook.start_reflection(days=days)
    except interviewer_playbook.PlaybookError as exc:
        raise _playbook_error(exc) from exc
    await _log_admin_action(db, admin, "REFLECT_PLAYBOOK", detail={"days": days})
    return {"success": True, "run": run}

@router.post("/interviewer-playbook", status_code=201)
async def add_playbook_entry(
    payload: PlaybookEntryRequest, admin: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    """A lesson written by hand; it is PROPOSED like the reflector's and needs approval."""
    try:
        entry_id = await interviewer_playbook.add_manual(
            db,
            step=payload.step,
            text_value=payload.text,
            skill=payload.skill,
            actor_id=str(admin.get("sub") or ""),
        )
    except interviewer_playbook.PlaybookError as exc:
        raise _playbook_error(exc) from exc
    await _log_admin_action(
        db, admin, "ADD_PLAYBOOK_ENTRY", target_id=entry_id, detail={"step": payload.step}
    )
    return {"success": True, "id": entry_id}

@router.post("/interviewer-playbook/{entry_id}/decision")
async def decide_playbook_entry(
    entry_id: str,
    payload: PlaybookDecisionRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Approve (into the prompts), reject a proposal, or retire an active lesson."""
    try:
        result = await interviewer_playbook.decide(
            db,
            entry_id[:36],
            decision=payload.decision,
            actor_id=str(admin.get("sub") or ""),
            note=payload.note,
            text_value=payload.text,
        )
    except interviewer_playbook.PlaybookError as exc:
        raise _playbook_error(exc) from exc
    await _log_admin_action(
        db,
        admin,
        "DECIDE_PLAYBOOK_ENTRY",
        target_id=entry_id[:36],
        detail={"decision": payload.decision, **result},
    )
    return {"success": True, **result}

@router.get("/ai/evaluation")
async def get_admin_ai_evaluation(
    days: int = Query(7, ge=1, le=90),
    refresh: bool = Query(False),
    _: dict = Depends(require_admin),
) -> dict:
    """Summary of the Langfuse project: judge and reviewer rates, review queue, golden-set runs."""
    return {"success": True, **await ai_overview.evaluation_view(days=days, refresh=refresh)}

@router.get("/ai/cost")
async def get_admin_ai_cost(
    days: int = Query(7, ge=1, le=90),
    refresh: bool = Query(False),
    _: dict = Depends(require_admin),
) -> dict:
    """LLM cost from Langfuse: total, per model, per feature, per day."""
    return {"success": True, **await ai_overview.cost_view(days=days, refresh=refresh)}

@router.get("/ai/speech")
async def get_admin_ai_speech(
    days: int = Query(7, ge=1, le=90),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """How the interviewer's speech was controlled: guard outcomes, reasons, sources, style."""
    try:
        speech = await interview_speech_control(db, days=days)
    except Exception as exc:  # tables missing until migration 0029 is applied
        await db.rollback()
        raise HTTPException(status_code=503, detail="AI observability tables are not migrated") from exc
    return {"success": True, **speech}




@router.post("/ai/speech/reviews")
async def review_admin_ai_speech_check(
    body: SpeechCheckReview,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """An admin's verdict on a refused or edited line: blocked correctly, or a good line blocked."""
    try:
        saved = await review_interview_speech_check(
            db,
            event_id=body.event_id,
            check_index=body.check_index,
            verdict=body.verdict,
            admin_id=str(admin.get("sub") or "") or None,
        )
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(status_code=503, detail="AI observability tables are not migrated") from exc
    if saved is None:
        raise HTTPException(status_code=404, detail="Speech check not found")
    return {"success": True, **saved}

@router.get('/ai/calls')
async def list_admin_ai_calls(purpose: str | None=Query(None, max_length=160), status: Literal['ok', 'error'] | None=Query(None), session_id: str | None=Query(None, alias='sessionId', max_length=36), days: int=Query(7, ge=1, le=90), limit: int=Query(50, ge=1, le=200), offset: int=Query(0, ge=0), _: dict=Depends(require_admin), db: AsyncSession=Depends(get_db)) -> dict:
    try:
        return await list_ai_calls_view(db, purpose=purpose, status=status, session_id=session_id, days=days, limit=limit, offset=offset)
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=503, detail='AI observability tables are not migrated') from exc

@router.get('/ai/calls/{call_id}')
async def get_admin_ai_call(call_id: int, _: dict=Depends(require_admin), db: AsyncSession=Depends(get_db)) -> dict:
    try:
        result = await get_ai_call_view(db, call_id)
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=503, detail='AI observability tables are not migrated') from exc
    if result is None:
        raise HTTPException(status_code=404, detail='AI call not found')
    return result

_SPEECH_STT_ADAPTERS = ["AssemblyAI (LiveKit Inference)", "Qwen3-ASR (streaming)"]

_SPEECH_TTS_ADAPTERS = ["ElevenLabs", "VieNeu", "Cartesia (LiveKit Inference)"]


async def _authenticate_speech_websocket(websocket: WebSocket, db: AsyncSession) -> bool:
    protocols = [part.strip() for part in websocket.headers.get("sec-websocket-protocol", "").split(",")]
    token = protocols[1] if len(protocols) == 2 and protocols[0] == "bearer" else ""
    import jwt

    from src.core.config import get_settings

    try:
        settings = get_settings()
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        user = (
            await db.execute(
                text(
                    "SELECT u.is_active, COALESCE(array_agg(ura.role) FILTER (WHERE ura.role IS NOT NULL), "
                    "ARRAY[]::text[]) AS roles FROM users u LEFT JOIN user_role_assignments ura "
                    "ON ura.user_id = u.id WHERE u.id = :user_id GROUP BY u.is_active"
                ),
                {"user_id": str(claims["sub"])},
            )
        ).mappings().one_or_none()
    except Exception:  # noqa: BLE001 - close unauthenticated WebSocket handshakes uniformly
        await websocket.close(code=4401)
        return False
    if user is None or not user["is_active"]:
        await websocket.close(code=4401)
        return False
    if "ADMIN" not in user["roles"]:
        await websocket.close(code=4403)
        return False
    await websocket.accept(subprotocol="bearer")
    return True


def _pcm16_wav(data: bytes, sample_rate: int, channels: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(data)
    return output.getvalue()

def _configured_tts_adapter() -> tuple[Any, str, str]:
    from livekit.agents import inference
    from livekit.plugins import elevenlabs

    from src.modules.interviews.adapters.voice.provider_config import tts_provider_config
    from src.modules.interviews.adapters.voice.vieneu_tts import VieNeuTTS

    provider_name = os.getenv("VOICE_LAB_TTS_PROVIDER", "elevenlabs").strip().lower()
    voice_id = os.getenv("ELEVENLABS_VI_VOICE_ID", "").strip() or os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    if provider_name in {"vieneu", "vieneu-tts"}:
        try:
            timeout_seconds = float(os.getenv("VIENEU_TTS_TIMEOUT_SECONDS", "180"))
        except ValueError:
            timeout_seconds = 180.0
        sample_rate = int(os.getenv("VIENEU_TTS_SAMPLE_RATE", "16000"))
        adapter = VieNeuTTS(
            base_url=os.getenv("VIENEU_TTS_BASE_URL", "http://host.docker.internal:8000"),
            api_key=os.getenv("VIENEU_TTS_API_KEY", "").strip() or os.getenv("VIENEU_API_KEY", "").strip(),
            voice=os.getenv("VIENEU_TTS_VOICE", "").strip(),
            model=os.getenv("VIENEU_TTS_MODEL", "").strip(),
            timeout_seconds=timeout_seconds,
            sample_rate=sample_rate,
        )
        model, provider = adapter.model, "vieneu"
    elif provider_name == "cartesia":
        model = os.getenv("VOICE_LAB_FALLBACK_TTS_MODEL", "cartesia/sonic-3").strip()
        voice = os.getenv("VOICE_LAB_FALLBACK_TTS_VOICE", "").strip()
        if not model or not os.getenv("LIVEKIT_API_KEY") or not os.getenv("LIVEKIT_API_SECRET"):
            raise HTTPException(status_code=503, detail="Cartesia LiveKit Inference is not configured")
        adapter = inference.TTS(model=model, **({"voice": voice} if voice else {}))
        provider = "cartesia"
    else:
        provider_name = "elevenlabs"
        config = tts_provider_config("vi-VN")
        if not voice_id or not (os.getenv("ELEVENLABS_API_KEY") or os.getenv("ELEVEN_API_KEY")):
            raise HTTPException(status_code=503, detail="ElevenLabs credentials or Vietnamese voice are not configured")
        adapter = elevenlabs.TTS(
            voice_id=voice_id,
            model=config.model,
            **({"language": config.language} if config.language else {}),
            auto_mode=config.auto_mode,
            apply_text_normalization=config.apply_text_normalization,
            voice_settings=elevenlabs.VoiceSettings(
                stability=config.stability,
                similarity_boost=config.similarity_boost,
                style=config.style,
                speed=config.speed,
                use_speaker_boost=config.use_speaker_boost,
            ),
        )
        model, provider = config.model, "elevenlabs"
    return adapter, provider, model


async def _synthesize_with_configured_adapter(text: str) -> tuple[bytes, int, int, str, str]:
    adapter, provider, model = _configured_tts_adapter()
    try:
        audio = await adapter.synthesize(text).collect()
        return bytes(audio.data), int(audio.sample_rate), int(audio.num_channels), provider, model
    finally:
        await adapter.aclose()

@router.post("/ai/speech/test/speak")
async def admin_speech_test_speak(
    payload: AdminSpeechTestRequest,
    _: dict = Depends(require_admin),
) -> Response:
    if not payload.text.strip():
        raise HTTPException(status_code=422, detail="Text must not be blank")
    started = perf_counter()
    audio, sample_rate, channels, provider, model = await _synthesize_with_configured_adapter(payload.text.strip())
    return Response(
        _pcm16_wav(audio, sample_rate, channels),
        media_type="audio/wav",
        headers={
            "X-Voice-Lab-Elapsed-Ms": str(round((perf_counter() - started) * 1000)),
            "X-Speech-Provider": provider,
            "X-Speech-Model": model,
        },
    )


@router.websocket("/ai/speech/test/speak/stream")
async def admin_speech_test_speak_stream(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Stream synthesized PCM frames to the browser as the TTS adapter emits them."""
    if not await _authenticate_speech_websocket(websocket, db):
        return
    adapter = None
    audio_stream = None
    started = perf_counter()
    try:
        request = await websocket.receive_json()
        text_to_speak = str(request.get("text") or "").strip()
        if not text_to_speak or len(text_to_speak) > 4096:
            await websocket.send_json({"type": "error", "detail": "Text must contain 1 to 4096 characters"})
            return
        adapter, provider, model = _configured_tts_adapter()
        await websocket.send_json({"type": "ready", "provider": provider, "model": model})
        # TTS.stream() is the incremental-text API and takes no text argument.
        # synthesize(text) is the one-shot-text API and still yields audio frames
        # progressively, which is what this test panel needs.
        audio_stream = adapter.synthesize(text_to_speak)
        async for event in audio_stream:
            frame = getattr(event, "frame", None)
            if frame is None:
                continue
            await websocket.send_json(
                {"type": "audio", "sampleRate": frame.sample_rate, "channels": frame.num_channels}
            )
            await websocket.send_bytes(bytes(frame.data))
        await websocket.send_json({"type": "done", "elapsedMs": round((perf_counter() - started) * 1000)})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - report provider errors to the test UI
        try:
            await websocket.send_json({"type": "error", "detail": str(exc)[:300]})
        except Exception:  # noqa: BLE001
            pass
    finally:
        if audio_stream is not None:
            try:
                await audio_stream.aclose()
            except Exception:  # noqa: BLE001
                pass
        if adapter is not None:
            await adapter.aclose()
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass

@router.post("/ai/speech/test/transcribe")
async def admin_speech_test_transcribe(
    audio: UploadFile = File(...),
    context: str = Form("", max_length=4000),
    reference_text: str = Form("", alias="referenceText", max_length=16000),
    _: dict = Depends(require_admin),
) -> dict:
    from .stt_test import SttTestOptions, transcribe_file

    return await transcribe_file(audio, SttTestOptions(context=context, referenceText=reference_text))


@router.websocket("/ai/speech/test/transcribe/stream")
async def admin_speech_test_transcribe_stream(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
) -> None:
    if not await _authenticate_speech_websocket(websocket, db):
        return
    from .stt_test import transcribe_stream

    await transcribe_stream(websocket)


@router.get("/ai/speech/test/config")
async def get_admin_speech_test_config(_: dict = Depends(require_admin)) -> dict:
    """Show integrated voice adapters and the providers selected by configuration."""
    from src.core.config import get_settings
    from src.modules.interviews.adapters.voice.provider_config import tts_provider_config
    from src.modules.interviews.adapters.voice.stt_provider import stt_provider_settings

    stt = stt_provider_settings()
    tts_provider = os.getenv("VOICE_LAB_TTS_PROVIDER", "elevenlabs").strip().lower()
    if tts_provider == "vieneu-tts":
        tts_provider = "vieneu"
    if tts_provider == "vieneu":
        tts_model = os.getenv("VIENEU_TTS_MODEL", "").strip() or "VieNeu (self-hosted)"
        tts_voice = os.getenv("VIENEU_TTS_VOICE", "").strip() or "default"
        tts_ready = bool(os.getenv("VIENEU_TTS_BASE_URL", "http://host.docker.internal:8000").strip())
    elif tts_provider == "cartesia":
        tts_model = os.getenv("VOICE_LAB_FALLBACK_TTS_MODEL", "cartesia/sonic-3").strip()
        tts_voice = os.getenv("VOICE_LAB_FALLBACK_TTS_VOICE", "").strip() or "default"
        tts_ready = bool(os.getenv("LIVEKIT_API_KEY") and os.getenv("LIVEKIT_API_SECRET") and tts_model)
    else:
        tts_provider = "elevenlabs"
        tts_config = tts_provider_config("vi-VN")
        tts_model = tts_config.model
        tts_voice = os.getenv("ELEVENLABS_VI_VOICE_ID", "").strip() or os.getenv("ELEVENLABS_VOICE_ID", "").strip()
        tts_ready = bool((os.getenv("ELEVENLABS_API_KEY") or os.getenv("ELEVEN_API_KEY")) and tts_voice)
    stt_ready = (
        bool(stt.base_url)
        if stt.provider == "qwen"
        else bool(os.getenv("LIVEKIT_API_KEY") and os.getenv("LIVEKIT_API_SECRET"))
    )
    settings = get_settings()
    return {
        "success": True,
        "sttProvider": stt.provider,
        "sttModel": stt.model,
        "sttReady": stt_ready,
        "livekitSttReady": bool(settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret),
        "ttsProvider": tts_provider,
        "ttsModel": tts_model,
        "ttsVoice": tts_voice,
        "ttsReady": tts_ready,
        "sttAdapters": _SPEECH_STT_ADAPTERS,
        "ttsAdapters": _SPEECH_TTS_ADAPTERS,
    }
