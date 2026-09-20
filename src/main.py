from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import socketio
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.core.config import get_settings
from src.core.data_initializer import initialize_data
from src.infrastructure.database import SessionFactory, postgres_lifespan
from src.infrastructure.rabbitmq import rabbitmq_lifespan
from src.infrastructure.socketio import sio
from src.modules.auth import build_module as build_auth_module
from src.modules.admin_users import build_module as build_admin_users_module
from src.modules.chat import build_module as build_chat_module
from src.modules.health import build_module as build_health_module
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

del _signaling_events

MODULES = [
    build_health_module(),
    build_auth_module(),
    build_admin_users_module(),
    build_users_module(),
    build_user_cvs_module(),
    build_job_descriptions_module(),
    build_taxonomy_module(),
    build_matching_module(),
    build_question_bank_module(),
    build_sessions_module(),
    build_chat_module(),
    build_voice_module(),
    build_video_calls_module(),
    build_notifications_module(),
]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    async with postgres_lifespan():
        from src.infrastructure.database import engine
        from src.modules.question_bank.schema import create_question_bank_schema
        from src.modules.taxonomy.schema import create_taxonomy_schema

        await create_taxonomy_schema(engine)
        await create_question_bank_schema(engine)
        await initialize_data(SessionFactory)
        async with rabbitmq_lifespan():
            yield


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
