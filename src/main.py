from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.core.config import get_settings
from src.infrastructure.database import postgres_lifespan
from src.infrastructure.rabbitmq import rabbitmq_lifespan
from src.infrastructure.socketio import sio
from src.modules.auth import build_module as build_auth_module
from src.modules.chat import build_module as build_chat_module
from src.modules.health import build_module as build_health_module
from src.modules.interview_questions import build_module as build_interview_questions_module
from src.modules.job_descriptions import build_module as build_job_descriptions_module
from src.modules.matching import build_module as build_matching_module
from src.modules.notifications import build_module as build_notifications_module
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
    build_users_module(),
    build_user_cvs_module(),
    build_job_descriptions_module(),
    build_taxonomy_module(),
    build_matching_module(),
    build_sessions_module(),
    build_chat_module(),
    build_interview_questions_module(),
    build_voice_module(),
    build_video_calls_module(),
    build_notifications_module(),
]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Development may start before RabbitMQ; fail-fast is intentional so readiness
    # accurately represents whether async job submission is usable.
    async with postgres_lifespan():
        async with rabbitmq_lifespan():
            yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Interview Prep API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    for app_module in MODULES:
        app.include_router(app_module.router)
    return app


fastapi_app = create_app()
asgi_app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app)
