"""Admin "Trạng thái hệ thống": is each piece the product depends on up right now?

Each check runs with a short timeout and reports ``ok`` | ``warn`` | ``down`` |
``off`` (not configured) with one line of detail, so a slow or missing
dependency never blocks the page:

* database: ``SELECT 1`` round trip, and the applied migration against the
  newest one in ``migrations/versions``;
* RabbitMQ: a passive declare of each work queue on a throwaway channel gives
  the messages waiting and the consumers listening. No consumer on a queue
  means no worker is taking that kind of job (CV parsing, JD parsing,
  matching), which is how a stopped worker shows up;
* LiveKit: the rooms open now and how many have people in them;
* voice agent: the newest interview event written by the voice room, since
  the agent registers with LiveKit and has no endpoint of its own;
* Langfuse: whether the project answers.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core import llm_tracing
from src.core.config import get_settings
from src.modules.admin.system.repository import database_snapshot, latest_voice_event

CHECK_TIMEOUT = 4.0
SLOW_DB_MS = 500
QUEUE_BACKLOG_WARN = 20
# An open room with people in it gets voice events every few seconds while the agent talks.
AGENT_IDLE_SECONDS = 300
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations" / "versions"
_REVISION_RE = re.compile(r'^revision(?:\s*:\s*str)?\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
_DOWN_RE = re.compile(r'^down_revision(?:\s*:[^=]+)?\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def check(key: str, status: str, detail: str | None = None, **data: Any) -> dict[str, Any]:
    return {"key": key, "status": status, "detail": detail, "data": data}


def migration_head(directory: Path = MIGRATIONS_DIR) -> str | None:
    """The newest revision: the one no other migration builds on. None when the folder is absent."""
    if not directory.is_dir():
        return None
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in directory.glob("*.py"):
        source = path.read_text(encoding="utf-8", errors="ignore")
        found = _REVISION_RE.search(source)
        if found:
            revisions.add(found.group(1))
        down = _DOWN_RE.search(source)
        if down:
            parents.add(down.group(1))
    heads = sorted(revisions - parents)
    return heads[-1] if heads else None


async def database_check(db: AsyncSession) -> dict[str, Any]:
    latency, applied = await database_snapshot(db)
    head = migration_head()
    behind = bool(head and applied and applied != head)
    status = "warn" if behind or latency > SLOW_DB_MS else "ok"
    detail = "migration_behind" if behind else ("slow" if latency > SLOW_DB_MS else None)
    return check("database", status, detail, latencyMs=latency, applied=applied, head=head)


def _queues() -> list[tuple[str, str]]:
    s = get_settings()
    return [("cv", s.rabbitmq_cv_queue), ("jd", s.rabbitmq_jd_queue), ("matching", s.rabbitmq_matching_queue)]


def queue_status(queues: list[dict[str, Any]]) -> tuple[str, str | None]:
    """``down`` when a queue has no consumer (no worker), ``warn`` on a backlog or a missing queue."""
    if any(q.get("consumers") == 0 for q in queues):
        return "down", "no_worker"
    if any(q.get("missing") for q in queues):
        return "warn", "queue_missing"
    if any((q.get("messages") or 0) >= QUEUE_BACKLOG_WARN for q in queues):
        return "warn", "backlog"
    return "ok", None


async def rabbitmq_check() -> dict[str, Any]:
    from src.infrastructure.rabbitmq import rabbitmq

    connection = rabbitmq.connection
    if connection is None or connection.is_closed:
        return check("rabbitmq", "down", "not_connected", queues=[])
    queues: list[dict[str, Any]] = []
    for key, name in _queues():
        # A passive declare of a missing queue closes its channel: one channel per queue.
        channel = await connection.channel()
        try:
            queue = await channel.declare_queue(name, passive=True)
            result = queue.declaration_result
            queues.append(
                {
                    "key": key,
                    "name": name,
                    "messages": int(result.message_count or 0),
                    "consumers": int(result.consumer_count or 0),
                }
            )
        except Exception:  # noqa: BLE001 - queue not declared yet (no worker ever started)
            queues.append({"key": key, "name": name, "messages": None, "consumers": None, "missing": True})
        finally:
            if not channel.is_closed:
                await channel.close()
    status, detail = queue_status(queues)
    return check("rabbitmq", status, detail, queues=queues)


async def livekit_check() -> dict[str, Any]:
    s = get_settings()
    if not (s.livekit_url and s.livekit_api_key and s.livekit_api_secret):
        return check("livekit", "off", "not_configured")
    from livekit import api

    client = api.LiveKitAPI(s.livekit_url, s.livekit_api_key, s.livekit_api_secret)
    try:
        response = await client.room.list_rooms(api.ListRoomsRequest())
        rooms = list(getattr(response, "rooms", []) or [])
    finally:
        await client.aclose()
    active = sum(1 for room in rooms if int(getattr(room, "num_participants", 0) or 0) > 0)
    return check("livekit", "ok", None, rooms=len(rooms), activeRooms=active)


async def voice_agent_check(db: AsyncSession) -> dict[str, Any]:
    # Events the agent writes every turn; an IN list uses the (event, created_at) index.
    last = await latest_voice_event(db)
    if last is None:
        return check("voiceAgent", "warn", "never_seen", lastEventAt=None)
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return check("voiceAgent", "ok", None, lastEventAt=last.isoformat())


def agent_status(
    voice: dict[str, Any], livekit: dict[str, Any], now: datetime | None = None
) -> dict[str, Any]:
    """Rooms with people in them but no voice event for a while: the agent is likely not running."""
    active = (livekit.get("data") or {}).get("activeRooms") or 0
    last = (voice.get("data") or {}).get("lastEventAt")
    if not active or voice["status"] == "down":
        return voice
    idle = True
    if last:
        moment = datetime.fromisoformat(last)
        idle = ((now or datetime.now(UTC)) - moment).total_seconds() > AGENT_IDLE_SECONDS
    if idle:
        return {**voice, "status": "warn", "detail": "rooms_without_agent"}
    return voice


def _llm_connections() -> list[str] | None:
    """Providers of the project's LLM connections (what the AI judges call); None when unknown."""
    lf = llm_tracing.client()
    if lf is None:
        return None
    try:
        return [str(c.provider) for c in lf.api.llm_connections.list(limit=50).data]
    except Exception:  # noqa: BLE001 - older Langfuse or key without the scope
        return None


def langfuse_status(url: str | None, providers: list[str] | None) -> tuple[str, str | None]:
    """ok / down (unreachable).

    The project API lists only project-level LLM connections; an organization
    connection (Settings → LLM Connections → Organization connections) also
    serves the judges but is not visible here, so an empty list is not a
    problem to report. The page shows what the project list contains.
    """
    if not url:
        return "down", "unreachable"
    return "ok", None


async def langfuse_check() -> dict[str, Any]:
    if not llm_tracing.enabled():
        return check("langfuse", "off", "not_configured")
    url = await asyncio.to_thread(llm_tracing.project_url)
    providers = await asyncio.to_thread(_llm_connections) if url else None
    status, detail = langfuse_status(url, providers)
    return check("langfuse", status, detail, projectUrl=url, llmConnections=providers)


async def guarded(key: str, probe: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Run one check; a timeout or an exception becomes a ``down`` result instead of an error page."""
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(probe(), timeout=CHECK_TIMEOUT)
    except TimeoutError:
        result = check(key, "down", "timeout")
    except ImportError:
        result = check(key, "off", "not_installed")
    except Exception as exc:  # noqa: BLE001
        result = check(key, "down", "error", error=f"{type(exc).__name__}: {exc}"[:200])
    result["checkedMs"] = round((time.monotonic() - started) * 1000)
    return result


async def health_view(db: AsyncSession) -> dict[str, Any]:
    # The two database checks share one session, so they run one after the other.
    database = await guarded("database", lambda: database_check(db))
    voice = await guarded("voiceAgent", lambda: voice_agent_check(db))
    others = await asyncio.gather(
        guarded("rabbitmq", rabbitmq_check),
        guarded("livekit", livekit_check),
        guarded("langfuse", langfuse_check),
    )
    rabbit, livekit, langfuse = others
    voice = agent_status(voice, livekit)
    checks = [database, rabbit, livekit, voice, langfuse]
    order = {"down": 0, "warn": 1, "ok": 2, "off": 3}
    worst = min((c["status"] for c in checks if c["status"] != "off"), key=order.__getitem__, default="ok")
    return {"overall": worst, "checks": checks, "checkedAt": datetime.now(UTC).isoformat()}
