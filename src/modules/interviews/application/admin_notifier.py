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

The same tick also tells admins about work waiting for their decision (see
``review_items``): a question sent for review, AI-generated questions entering
the review queue, a proposed interviewer-playbook lesson, and new suggestions
from the bank analysis. The person who wrote an item is not notified about it,
since they cannot approve their own work.

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
REVIEW_NOTIFICATION_TYPE = "admin.review"
# Only work that arrived recently is announced, so the first run after a
# deploy does not flood the bell with an old backlog.
REVIEW_WINDOW_DAYS = 3
REVIEW_LIMIT = 20
QUOTE_CHARS = 120
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


def _quote(value: Any) -> str:
    body = " ".join(str(value or "").split())
    return body if len(body) <= QUOTE_CHARS else body[: QUOTE_CHARS - 1].rstrip() + "…"


def _stamp(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value or "")


def review_items(
    *,
    questions: list[dict[str, Any]],
    generated: dict[str, Any] | None,
    lessons: list[dict[str, Any]],
    suggestions: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Notifications for work waiting on an admin's decision.

    Pure (unit tested). Each question submission and each proposed lesson gives
    one item; AI-generated questions and bank suggestions arrive in batches, so
    each batch gives one item keyed by its newest row. ``exclude`` is the
    author, who is not notified about their own work.
    """
    items: list[dict[str, Any]] = []
    for row in questions:
        version_id = str(row.get("version_id") or "")
        question_id = str(row.get("question_id") or "")
        if not version_id or not question_id:
            continue
        who = row.get("author") or "Người soạn"
        items.append(
            {
                "dedupe_key": f"review:question:{version_id}:{_stamp(row.get('submitted_at'))}"[:160],
                "title": f"Câu hỏi chờ duyệt · {row.get('stable_key') or version_id[:8]}"[:255],
                "message": f"{who} gửi duyệt: “{_quote(row.get('text'))}”. "
                "Mở câu để ghi ý kiến rồi phê duyệt cuối.",
                "exclude": str(row.get("created_by") or ""),
                "data": {
                    "kind": "review_question",
                    "severity": "info",
                    "questionId": question_id,
                    "link": f"/admin/question-bank/{question_id}",
                },
            }
        )
    if generated and int(generated.get("count") or 0) > 0:
        items.append(
            {
                "dedupe_key": f"review:generated:{_stamp(generated.get('latest'))}"[:160],
                "title": f"{generated['count']} câu do AI sinh đang chờ duyệt",
                "message": "Các câu này đã có thể được hỏi trong buổi phỏng vấn trước khi có người duyệt. "
                "Mở Ngân hàng câu hỏi, mục Việc của tôi.",
                "exclude": "",
                "data": {"kind": "review_generated", "severity": "warning", "link": "/admin/question-bank"},
            }
        )
    for row in lessons:
        entry_id = str(row.get("id") or "")
        if not entry_id:
            continue
        manual = row.get("origin") == "manual"
        who = (row.get("author") or "Một admin") if manual else "AI"
        items.append(
            {
                "dedupe_key": f"review:playbook:{entry_id}"[:160],
                "title": "Bài học sổ tay chờ duyệt" + (" · sửa bài cũ" if row.get("replaces_id") else ""),
                "message": f"{who} đề xuất: “{_quote(row.get('text'))}”.",
                "exclude": str(row.get("created_by") or "") if manual else "",
                "data": {"kind": "review_playbook", "severity": "info", "link": "/admin/ai/playbook"},
            }
        )
    if suggestions and int(suggestions.get("count") or 0) > 0:
        items.append(
            {
                "dedupe_key": f"review:suggestions:{_stamp(suggestions.get('latest'))}"[:160],
                "title": f"{suggestions['count']} đề xuất cho ngân hàng câu hỏi",
                "message": "Từ phân tích dữ liệu trả lời: câu quá dễ, quá khó, "
                "phân hoá kém hoặc cần sửa barem.",
                "exclude": "",
                "data": {
                    "kind": "review_suggestions",
                    "severity": "info",
                    "link": "/admin/question-bank/insights",
                },
            }
        )
    return items


async def load_review_work(db: AsyncSession) -> list[dict[str, Any]]:
    """Recent work waiting for an admin, as ``review_items``."""
    from src.modules.question_bank.facade import GENERATED_QUESTION_AUTHOR

    window = {"days": REVIEW_WINDOW_DAYS, "limit": REVIEW_LIMIT}
    since = "now() - make_interval(days => :days)"
    questions = (
        (
            await db.execute(
                text(
                    "SELECT qv.id AS version_id, qv.question_id, q.stable_key, qv.canonical_text AS text, "
                    "qv.created_by, COALESCE(qv.submitted_at, qv.created_at) AS submitted_at, "
                    "COALESCE(u.name, u.email) AS author "
                    "FROM interview_question_versions qv JOIN interview_questions q ON q.id = qv.question_id "
                    "LEFT JOIN users u ON CAST(u.id AS TEXT) = qv.created_by "
                    "WHERE qv.status IN ('IN_REVIEW', 'PILOT_READY') AND q.retired_at IS NULL "
                    "AND qv.created_by <> :generator "
                    f"AND COALESCE(qv.submitted_at, qv.created_at) >= {since} "
                    "ORDER BY 6 DESC LIMIT :limit"
                ),
                {**window, "generator": GENERATED_QUESTION_AUTHOR},
            )
        )
        .mappings()
        .all()
    )
    generated = (
        (
            await db.execute(
                text(
                    "SELECT count(*) AS count, max(COALESCE(qv.submitted_at, qv.created_at)) AS latest "
                    "FROM interview_question_versions qv JOIN interview_questions q ON q.id = qv.question_id "
                    "WHERE qv.status IN ('IN_REVIEW', 'PILOT_READY') AND q.retired_at IS NULL "
                    "AND qv.created_by = :generator "
                    f"HAVING max(COALESCE(qv.submitted_at, qv.created_at)) >= {since}"
                ),
                {"days": REVIEW_WINDOW_DAYS, "generator": GENERATED_QUESTION_AUTHOR},
            )
        )
        .mappings()
        .first()
    )
    lessons = (
        (
            await db.execute(
                text(
                    "SELECT p.id, p.text, p.origin, p.replaces_id, p.created_by, "
                    "COALESCE(u.name, u.email) AS author "
                    "FROM interviewer_playbook p LEFT JOIN users u ON CAST(u.id AS TEXT) = p.created_by "
                    f"WHERE p.status = 'PROPOSED' AND p.created_at >= {since} "
                    "ORDER BY p.created_at DESC LIMIT :limit"
                ),
                window,
            )
        )
        .mappings()
        .all()
    )
    suggestions = (
        (
            await db.execute(
                text(
                    "SELECT count(*) AS count, max(created_at) AS latest FROM question_bank_suggestions "
                    f"WHERE status = 'OPEN' HAVING max(created_at) >= {since}"
                ),
                {"days": REVIEW_WINDOW_DAYS},
            )
        )
        .mappings()
        .first()
    )
    return review_items(
        questions=[dict(row) for row in questions],
        generated=dict(generated) if generated else None,
        lessons=[dict(row) for row in lessons],
        suggestions=dict(suggestions) if suggestions else None,
    )


_INSERT = """
    INSERT INTO notifications (id, user_id, type, title, message, data, dedupe_key)
    SELECT md5(:dedupe_key || ':' || u.id)::uuid::text, u.id, :type, :title, :message,
           CAST(:data AS JSONB), :dedupe_key
    FROM users u
    WHERE u.is_active AND CAST(u.id AS TEXT) <> :exclude AND EXISTS (
        SELECT 1 FROM user_role_assignments r WHERE r.user_id = u.id AND r.role = 'ADMIN'
    )
    ON CONFLICT (user_id, dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
    RETURNING id
"""


async def sync_admin_notifications(db: AsyncSession) -> list[dict[str, Any]]:
    """Create the notifications for new problems; returns the items that were new."""
    alerts = (await load_alerts(db))["alerts"]
    items = [{**item, "type": NOTIFICATION_TYPE} for item in notification_items(alerts)]
    try:
        items += [{**item, "type": REVIEW_NOTIFICATION_TYPE} for item in await load_review_work(db)]
    except Exception:  # noqa: BLE001 - a missing table must not stop the AI alerts
        await db.rollback()
        logger.warning("Review notifications not evaluated", exc_info=True)
    created: list[dict[str, Any]] = []
    try:
        for item in items:
            rows = (
                await db.execute(
                    text(_INSERT),
                    {
                        "dedupe_key": item["dedupe_key"],
                        "type": item["type"],
                        "exclude": item.get("exclude") or "",
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
