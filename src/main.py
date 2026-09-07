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
from src.modules.health import build_module as build_health_module
from src.modules.job_categories import build_module as build_job_categories_module
from src.modules.job_profiles import build_module as build_job_profiles_module
from src.modules.matching import build_module as build_matching_module
from src.modules.sessions import build_module as build_sessions_module
from src.modules.users import build_module as build_users_module
from src.modules.user_cvs import build_module as build_user_cvs_module
from src.modules.video_calls import build_module as build_video_calls_module

MODULES = [
    build_health_module(),
    build_auth_module(),
    build_users_module(),
    build_user_cvs_module(),
    build_job_categories_module(),
    build_job_profiles_module(),
    build_matching_module(),
    build_sessions_module(),
    build_video_calls_module(),
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
