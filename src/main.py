import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import socketio
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from src.core.config import get_settings
from src.infrastructure.database import SessionFactory, postgres_lifespan
from src.infrastructure.rabbitmq import rabbitmq_lifespan
from src.infrastructure.socketio import sio
from src.modules.admin import build_module as build_admin_module
from src.modules.admin_users import build_module as build_admin_users_module
from src.modules.auth import build_module as build_auth_module
from src.modules.chat import build_module as build_chat_module
from src.modules.health import build_module as build_health_module
from src.modules.interviews import build_module as build_interviews_module
from src.modules.job_descriptions import build_module as build_job_descriptions_module
from src.modules.matching import build_module as build_matching_module
from src.modules.notifications import build_module as build_notifications_module
from src.modules.question_bank import build_module as build_question_bank_module
from src.modules.sessions import build_module as build_sessions_module
from src.modules.signaling import _events as _signaling_events
from src.modules.taxonomy import build_module as build_taxonomy_module
from src.modules.user_cvs import build_module as build_user_cvs_module
from src.modules.users import build_module as build_users_module
from src.modules.video_calls import build_module as build_video_calls_module
from src.modules.voice import build_module as build_voice_module
from src.seeds import run_all_seeds

del _signaling_events

MODULES = [
    build_health_module(),
    build_auth_module(),
    build_admin_module(),
    build_admin_users_module(),
    build_users_module(),
    build_user_cvs_module(),
    build_job_descriptions_module(),
    build_taxonomy_module(),
    build_matching_module(),
    build_question_bank_module(),
    build_interviews_module(),
    build_sessions_module(),
    build_chat_module(),
    build_voice_module(),
    build_video_calls_module(),
    build_notifications_module(),
]

SCHEMA_BOOTSTRAP_LOCK_KEY = 761_098_241
logger = logging.getLogger(__name__)


async def _start_livekit_agent(settings: object) -> asyncio.subprocess.Process | None:
    """Start the LiveKit worker as a child of the API process when enabled."""
    if not getattr(settings, "livekit_agent_autostart", False):
        return None
    if not all(
        getattr(settings, name, None)
        for name in ("voice_lab_enabled", "livekit_url", "livekit_api_key", "livekit_api_secret")
    ):
        logger.warning(
            "LiveKit agent autostart is enabled but Voice Lab/LiveKit configuration is incomplete; "
            "the worker was not started"
        )
        return None

    run_mode = str(getattr(settings, "livekit_agent_run_mode", "dev")).strip().lower()
    if run_mode not in {"dev", "start"}:
        raise RuntimeError("LIVEKIT_AGENT_RUN_MODE must be 'dev' or 'start'")

    agent_script = Path(__file__).resolve().parent / "modules" / "voice" / "livekit_agent.py"
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(agent_script),
            run_mode,
            cwd=str(agent_script.parents[3]),
        )
    except OSError:
        logger.exception("Could not start the LiveKit agent process")
        return None

    logger.info("LiveKit agent started automatically (pid=%s, mode=%s)", process.pid, run_mode)
    return process


async def _stop_livekit_agent(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def bootstrap_question_bank_schema(engine: object) -> None:
    """Serialize create_all bootstrap across concurrently starting replicas."""
    from src.modules.question_bank.schema import create_question_bank_schema
    from src.modules.taxonomy.schema import create_taxonomy_schema

    async with engine.connect() as connection:
        await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": SCHEMA_BOOTSTRAP_LOCK_KEY})
        try:
            await create_taxonomy_schema(engine)
            await create_question_bank_schema(engine)
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": SCHEMA_BOOTSTRAP_LOCK_KEY},
            )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    livekit_agent: asyncio.subprocess.Process | None = None
    async with postgres_lifespan():
        from src.infrastructure.database import engine

        await bootstrap_question_bank_schema(engine)
        await run_all_seeds(SessionFactory)
        async with rabbitmq_lifespan():
            livekit_agent = await _start_livekit_agent(get_settings())
            try:
                yield
            finally:
                await _stop_livekit_agent(livekit_agent)


def _message(detail: object) -> str:
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("detail") or "Yêu cầu không hợp lệ.")
    return str(detail)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Interview Prep API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"message": _message(exc.detail), "statusCode": exc.status_code},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        first_error = exc.errors()[0] if exc.errors() else {}
        message = str(first_error.get("msg") or "Dữ liệu không hợp lệ.")
        if message.startswith("Value error, "):
            message = message.removeprefix("Value error, ")
        return JSONResponse(
            status_code=422,
            content={"message": message, "statusCode": 422},
        )

    for app_module in MODULES:
        app.include_router(app_module.router)
    return app


fastapi_app = create_app()
asgi_app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app)
