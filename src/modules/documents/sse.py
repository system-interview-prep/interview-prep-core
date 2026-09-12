"""Small dependency-free Server-Sent Events primitives for job status streams."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any, Protocol


class DisconnectAwareRequest(Protocol):
    async def is_disconnected(self) -> bool: ...


StatusLoader = Callable[[], Awaitable[dict[str, Any]]]


def encode_sse(
    *, event: str | None = None, data: dict[str, Any] | None = None, comment: str | None = None
) -> str:
    """Serialize one safe SSE frame; payloads are always JSON objects."""
    if comment is not None:
        return f": {comment}\n\n"
    lines = []
    if event:
        lines.append(f"event: {event}")
    if data is not None:
        lines.append(f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"


async def status_event_stream(
    request: DisconnectAwareRequest,
    load_status: StatusLoader,
    *,
    poll_interval_seconds: float,
    heartbeat_seconds: float,
) -> Any:
    """Emit status deltas from the authoritative database state.

    Celery runs outside the API process, so an in-memory queue would drop
    cross-process updates. This stream deliberately reads the persisted state;
    the browser holds one connection instead of issuing repeated HTTP polls.
    """
    last_fingerprint: str | None = None
    last_heartbeat = monotonic()
    while not await request.is_disconnected():
        try:
            payload = await load_status()
        except Exception:
            yield encode_sse(event="error", data={"code": "status_unavailable"})
            return
        fingerprint = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if fingerprint != last_fingerprint:
            yield encode_sse(event="status", data=payload)
            last_fingerprint = fingerprint
            if payload["status"] in {"DONE", "FAILED"}:
                yield encode_sse(event="complete", data={"status": payload["status"]})
                return
        now = monotonic()
        if now - last_heartbeat >= heartbeat_seconds:
            yield encode_sse(comment="keep-alive")
            last_heartbeat = now
        await asyncio.sleep(poll_interval_seconds)


SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
