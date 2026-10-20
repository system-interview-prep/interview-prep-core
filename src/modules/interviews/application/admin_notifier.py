"""Notifications for admins when the AI pipeline needs attention.

The alerts (pipeline_board.load_alerts) describe problems as they are now.
This module turns each *new* problem into a row in ``notifications`` for every
active admin, once: a failed or stuck grading, an error during an interview, a
plan that never finished, a voice outage, an idle interview (once per
interview), and fallback rates, latency or model errors above their thresholds
(at most once per hour each).

It runs in the API process: every 30 seconds, and a couple of seconds after
the recorder reports a warning or an error, so a problem reaches the admin
pages within seconds. New notifications are announced on the admin socket
(``notification.new``); pages that miss it pick them up on their next poll.

Duplicates are prevented by ``notifications.dedupe_key`` (migration 0031):
the insert is ``ON CONFLICT DO NOTHING``, so concurrent API processes and
repeated evaluations of the same alert are harmless.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core import pipeline_events
from src.modules.interviews.application.pipeline_board import load_alerts

logger = logging.getLogger(__name__)

NOTIFICATION_TYPE = "admin.ai_alert"
INTERVAL_SECONDS = 30.0
# After a pushed problem, wait for the rest of the burst before evaluating.
SETTLE_SECONDS = 2.0

# Problems tied to one interview: notified once per interview.
SESSION_KINDS = frozenset(
    {"grading_failed", "grading_stuck", "live_errors", "plan_stuck", "voice_degraded", "interview_idle"}
)

_TEXT: dict[str, tuple[str, str]] = {
    "grading_failed": (
        "Chấm điểm lỗi · {who}",
        "{job}chưa có báo cáo cho ứng viên. Mở buổi để xem lỗi gần nhất.",
    ),
    "grading_stuck": ("Việc chấm bị treo · {who}", "{job}đang chấm nhưng đã quá hạn, worker có thể đã dừng."),
    "live_errors": ("Lỗi khi phỏng vấn · {who}", "{job}có lỗi trong {minutes} phút qua."),
    "plan_stuck": ("Kế hoạch bị kẹt · {who}", "{job}chưa lập xong sau {minutes} phút, ứng viên đang chờ."),
    "voice_degraded": (
        "Dịch vụ giọng nói lỗi · {who}",
        "{job}chưa hồi phục, ứng viên có thể không nghe được AI.",
    ),
    "interview_idle": (
        "Buổi phỏng vấn không hoạt động · {who}",
        "{job}hơn {minutes} phút không có trao đổi nào.",
    ),
    "ack_fallback_rate": (
        "Câu ghi nhận dự phòng {rate}",
        "{count}/{total} lượt trong {minutes} phút qua (ngưỡng {threshold}).",
    ),
    "probe_fallback_rate": (
        "Câu hỏi thêm dự phòng {rate}",
        "{count}/{total} câu hỏi thêm trong {minutes} phút qua (ngưỡng {threshold}).",
    ),
    "turn_latency": ("AI phản hồi chậm: p95 {p95}", "{count} lượt trong {minutes} phút qua, ngưỡng {limit}."),
    "ai_errors": ("{count} lần gọi AI lỗi", "Trong {minutes} phút qua ({rate} số lần gọi)."),
}


def _percent(value: Any) -> str:
    return f"{round(float(value) * 100)}%" if isinstance(value, (int, float)) else "—"


def _seconds(ms: Any) -> str:
    return f"{float(ms) / 1000:.1f} s" if isinstance(ms, (int, float)) else "—"


def notification_items(alerts: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    """The notifications the current alerts call for, each with its dedupe key.

    Pure (unit tested). Interview problems give one item per affected
    interview; rate and latency problems give one item per hour.
    """
    now = now or datetime.now(UTC)
    hour = now.strftime("%Y%m%d%H")
    items: list[dict[str, Any]] = []
    for alert in alerts:
        kind = alert.get("kind")
        if kind not in _TEXT:
            continue
        title_t, message_t = _TEXT[kind]
        values = {
            "count": alert.get("count", 0),
            "total": alert.get("total", 0),
            "rate": _percent(alert.get("rate")),
            "threshold": _percent(alert.get("threshold")),
            "minutes": alert.get("minutes", 0),
            "p95": _seconds(alert.get("p95Ms")),
            "limit": _seconds(alert.get("thresholdMs")),
        }
        severity = alert.get("severity", "warning")
        if kind in SESSION_KINDS:
            for session in alert.get("sessions") or []:
                sid = str(session.get("id") or "")
                if not sid:
                    continue
                job = session.get("jobTitle")
                text_values = {
                    **values,
                    "who": session.get("candidate") or sid[:8],
                    "job": f"{job}: " if job else "",
                }
                items.append(
                    {
                        "dedupe_key": f"ai_alert:{kind}:{sid}"[:160],
                        "title": title_t.format(**text_values)[:255],
                        "message": message_t.format(**text_values),
                        "data": {
                            "kind": kind,
                            "severity": severity,
                            "sessionId": sid,
                            "link": f"/admin/interviews/{sid}",
                        },
                    }
                )
        else:
            items.append(
                {
                    "dedupe_key": f"ai_alert:{kind}:{hour}",
                    "title": title_t.format(**values)[:255],
                    "message": message_t.format(**values),
                    "data": {
                        "kind": kind,
                        "severity": severity,
                        "link": "/admin/ai/errors" if kind == "ai_errors" else "/admin/ai",
                    },
                }
            )
    return items


_INSERT = """
    INSERT INTO notifications (id, user_id, type, title, message, data, dedupe_key)
    SELECT md5(:dedupe_key || ':' || u.id)::uuid::text, u.id, :type, :title, :message,
           CAST(:data AS JSONB), :dedupe_key
    FROM users u
    WHERE u.is_active AND EXISTS (
        SELECT 1 FROM user_role_assignments r WHERE r.user_id = u.id AND r.role = 'ADMIN'
    )
    ON CONFLICT (user_id, dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
    RETURNING id
"""


async def sync_admin_notifications(db: AsyncSession) -> list[dict[str, Any]]:
    """Create the notifications for new problems; returns the items that were new."""
    alerts = (await load_alerts(db))["alerts"]
    items = notification_items(alerts)
    created: list[dict[str, Any]] = []
    try:
        for item in items:
            rows = (
                await db.execute(
                    text(_INSERT),
                    {
                        "dedupe_key": item["dedupe_key"],
                        "type": NOTIFICATION_TYPE,
                        "title": item["title"],
                        "message": item["message"],
                        "data": json.dumps(item["data"], ensure_ascii=False),
                    },
                )
            ).all()
            if rows:
                created.append(item)
        await db.commit()
    except Exception:  # noqa: BLE001 - before migration 0031 there is no dedupe_key
        await db.rollback()
        logger.warning("Admin notifications not created", exc_info=True)
        return []
    return created


async def publish_notifications(items: list[dict[str, Any]]) -> None:
    """Announce new notifications to the open admin pages."""
    from src.infrastructure.socketio import sio
    from src.modules.interviews.application.pipeline_live import ALL_ROOM, NAMESPACE

    payload = {
        "count": len(items),
        "items": [
            {
                "title": item["title"],
                "message": item["message"],
                **{key: item["data"].get(key) for key in ("kind", "severity", "link")},
            }
            for item in items[:10]
        ],
    }
    with contextlib.suppress(Exception):
        await sio.emit("notification.new", payload, room=ALL_ROOM, namespace=NAMESPACE)


class AdminNotifier:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None

    def wake(self) -> None:
        """Evaluate soon (call from the API event loop)."""
        if self._wake is not None:
            self._wake.set()

    def _on_summaries(self, summaries: list[dict[str, Any]]) -> None:
        # Flusher thread: hop to the loop only when something went wrong.
        if not any(s.get("problems") for s in summaries):
            return
        loop = self._loop
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self.wake)

    async def _run(self) -> None:
        from src.infrastructure.database import SessionFactory

        assert self._wake is not None
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), INTERVAL_SECONDS)
            if self._wake.is_set():
                await asyncio.sleep(SETTLE_SECONDS)
            self._wake.clear()
            try:
                async with SessionFactory() as db:
                    created = await sync_admin_notifications(db)
                if created:
                    await publish_notifications(created)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the next tick retries
                logger.warning("Admin notifier tick failed", exc_info=True)

    async def start(self) -> None:
        if not pipeline_events.enabled():
            return
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        pipeline_events.add_live_listener(self._on_summaries)
        self._task = asyncio.create_task(self._run(), name="admin-notifier")

    async def stop(self) -> None:
        pipeline_events.remove_live_listener(self._on_summaries)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        self._loop = None
        self._wake = None


notifier = AdminNotifier()
