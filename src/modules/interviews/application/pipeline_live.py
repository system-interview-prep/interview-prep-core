"""Push pipeline activity to open admin pages (socket.io namespace /admin-pipeline).

Admin pages used to poll. Now, a few hundred milliseconds after the recorder
(src/core/pipeline_events.py) writes events or model calls, every connected
admin receives a short summary (room ``all``) and refetches what it shows; a
page following one interview ignores summaries of other sessions. Summaries
carry event names and counts only, never candidate text, and the volume is a
few per interview turn, so one room is enough.

Two sources feed the bridge:

1. the recorder in this API process, through a local listener (no database
   round trip);
2. ``LISTEN pipeline_events`` on a dedicated asyncpg connection, for writes
   made by other processes (the Celery grading worker). Payloads written by
   this process are skipped there because (1) already relayed them.

The connection reconnects with backoff; when it is down, pages keep their
polling fallback, so a lost socket never hides activity for long.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import jwt
import socketio
from sqlalchemy import text

from src.core import pipeline_events
from src.core.config import get_settings
from src.infrastructure.socketio import sio

logger = logging.getLogger(__name__)

NAMESPACE = "/admin-pipeline"
ALL_ROOM = "all"
EVENT = "pipeline.event"
_RECONNECT_SECONDS = (1, 2, 5, 10, 30)


async def _is_admin(user_id: str) -> bool:
    from src.infrastructure.database import SessionFactory

    async with SessionFactory() as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT u.is_active, EXISTS (SELECT 1 FROM user_role_assignments r "
                        "WHERE r.user_id = u.id AND r.role = 'ADMIN') AS is_admin "
                        "FROM users u WHERE u.id = :uid"
                    ),
                    {"uid": user_id},
                )
            )
            .mappings()
            .one_or_none()
        )
    return bool(row and row["is_active"] and row["is_admin"])


class AdminPipelineNamespace(socketio.AsyncNamespace):
    """Admins only: the same JWT the REST API takes, checked for the ADMIN role."""

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> bool:
        del environ
        token = str((auth or {}).get("token") or "").removeprefix("Bearer ").strip()
        try:
            settings = get_settings()
            claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
            user_id = str(claims["sub"])
        except (jwt.InvalidTokenError, KeyError):
            return False
        try:
            if not await _is_admin(user_id):
                return False
        except Exception:  # noqa: BLE001 - a database hiccup refuses, the page keeps polling
            logger.warning("Admin pipeline socket: role check failed", exc_info=True)
            return False
        await self.save_session(sid, {"user_id": user_id})
        await self.enter_room(sid, ALL_ROOM)
        return True


sio.register_namespace(AdminPipelineNamespace(NAMESPACE))


async def publish(summary: dict[str, Any]) -> None:
    """Send one session summary to every connected admin."""
    payload = {key: value for key, value in summary.items() if key != "o"}
    try:
        await sio.emit(EVENT, payload, room=ALL_ROOM, namespace=NAMESPACE)
    except Exception:  # noqa: BLE001
        logger.debug("Admin pipeline emit failed", exc_info=True)


def parse_notification(payload: str, *, own_instance: str) -> dict[str, Any] | None:
    """A pg_notify payload from another process, or None (own, or malformed)."""
    try:
        body = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("o") == own_instance:
        return None
    return body


def _listen_dsn(database_url: str) -> str:
    for prefix in ("postgresql+asyncpg://", "postgres://"):
        if database_url.startswith(prefix):
            return "postgresql://" + database_url[len(prefix) :]
    return database_url


class PipelineLiveBridge:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False

    # -- local writes (flusher thread) -------------------------------------
    def _on_local(self, summaries: list[dict[str, Any]]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        def schedule() -> None:
            for summary in summaries:
                asyncio.ensure_future(publish(summary))

        with contextlib.suppress(RuntimeError):  # loop closed during shutdown
            loop.call_soon_threadsafe(schedule)

    # -- writes from other processes -----------------------------------------
    async def _listen_forever(self) -> None:
        import asyncpg

        attempt = 0
        dsn = _listen_dsn(get_settings().database_url)
        while not self._stopping:
            connection = None
            try:
                connection = await asyncpg.connect(dsn)

                def on_notify(_conn, _pid, _channel, payload: str) -> None:
                    body = parse_notification(payload, own_instance=pipeline_events.INSTANCE_ID)
                    if body is not None:
                        asyncio.ensure_future(publish(body))
                        if body.get("problems"):
                            # A worker reported a problem: notify admins soon.
                            from src.modules.interviews.application.admin_notifier import notifier

                            notifier.wake()

                await connection.add_listener(pipeline_events.NOTIFY_CHANNEL, on_notify)
                attempt = 0
                while not self._stopping and not connection.is_closed():
                    await asyncio.sleep(5)
                    # Detects a silently dropped connection.
                    await connection.execute("SELECT 1")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                delay = _RECONNECT_SECONDS[min(attempt, len(_RECONNECT_SECONDS) - 1)]
                attempt += 1
                logger.warning("Admin pipeline LISTEN lost; retrying in %ss", delay, exc_info=attempt == 1)
                await asyncio.sleep(delay)
            finally:
                if connection is not None:
                    with contextlib.suppress(Exception):
                        await connection.close()

    async def start(self) -> None:
        if not pipeline_events.enabled():
            return
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        pipeline_events.add_live_listener(self._on_local)
        self._task = asyncio.create_task(self._listen_forever(), name="admin-pipeline-listen")

    async def stop(self) -> None:
        self._stopping = True
        pipeline_events.remove_live_listener(self._on_local)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        self._loop = None


bridge = PipelineLiveBridge()
