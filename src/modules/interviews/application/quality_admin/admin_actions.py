"""Admin interventions on one interview session.

* ``close_session``: end a session that is stuck open (the candidate left, the
  voice agent died). ``end_reason`` must stay inside the table's CHECK list, so
  an admin close is stored as ``TECHNICAL_FAILURE``; who closed it and why is in
  ``admin_access_logs`` and in the ``session_closed_by_admin`` pipeline event.
* ``start_grading``: grade a closed session that has no report (grading failed,
  or the candidate never opened the report page, which is what triggers it).
  Grading takes minutes, so it runs in the background on its own database
  session; the admin page follows it through the pipeline view. A session that
  already has a report is never re-graded here, because that would replace a
  result the candidate has already seen.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.trace_logging import trace_event
from src.infrastructure.database import SessionFactory
from src.modules.interviews.evaluation import evaluate_closed_session

logger = logging.getLogger(__name__)

ADMIN_CLOSE_REASON = "TECHNICAL_FAILURE"
_running: dict[str, asyncio.Task[None]] = {}


class AdminActionError(Exception):
    """A refused admin action; ``status_code`` is the HTTP status to return."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


async def _session(db: AsyncSession, session_id: str) -> dict[str, Any]:
    row = (
        (
            await db.execute(
                text("SELECT id, status, end_reason FROM interview_sessions WHERE id = :sid"),
                {"sid": session_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise AdminActionError(404, "not_found", "Không tìm thấy phiên phỏng vấn.")
    return dict(row)


async def close_session(db: AsyncSession, session_id: str, *, note: str | None = None) -> dict[str, Any]:
    """Close an open session; returns the status it had."""
    session = await _session(db, session_id)
    previous = str(session.get("status") or "")
    if previous.upper() == "CLOSED":
        raise AdminActionError(409, "already_closed", "Phiên này đã kết thúc.")
    result = await db.execute(
        text(
            "UPDATE interview_sessions "
            "SET status = 'CLOSED', end_reason = COALESCE(end_reason, :reason), "
            "ended_at = now(), updated_at = now() "
            "WHERE id = :sid AND status <> 'CLOSED'"
        ),
        {"sid": session_id, "reason": ADMIN_CLOSE_REASON},
    )
    await db.commit()
    if not result.rowcount:
        raise AdminActionError(409, "already_closed", "Phiên này đã kết thúc.")
    trace_event(
        "interviewer",
        "session_closed_by_admin",
        session_id=session_id,
        previous_status=previous,
        note=(note or "")[:200] or None,
    )
    return {"previousStatus": previous, "endReason": session.get("end_reason") or ADMIN_CLOSE_REASON}


# A turn the grader scores: a main question (not the greeting or the closing)
# that the candidate answered. A session without one cannot be graded
# (``evaluate_closed_session`` raises "không có câu hỏi nào để đánh giá").
_GRADABLE_TURN_CONDITION = """
    t.session_id = {session} AND UPPER(COALESCE(t.status, '')) = 'ANSWERED'
    AND COALESCE(t.question_snapshot->>'stage', '') NOT IN ('', 'WARM_UP', 'CLOSING')
"""
_GRADABLE_TURN_SQL = (
    "SELECT 1 FROM interview_turns t WHERE " + _GRADABLE_TURN_CONDITION.format(session=":sid") + " LIMIT 1"
)


def grading_running(session_id: str) -> bool:
    task = _running.get(session_id)
    return bool(task and not task.done())


async def grading_state(db: AsyncSession, session_id: str) -> dict[str, Any]:
    """Whether ``start_grading`` would be accepted, and why not."""
    session = await _session(db, session_id)
    if str(session.get("status") or "").upper() != "CLOSED":
        return {"allowed": False, "code": "not_closed"}
    if await db.scalar(
        text("SELECT 1 FROM interview_evaluations WHERE session_id = :sid"), {"sid": session_id}
    ):
        return {"allowed": False, "code": "already_graded"}
    if grading_running(session_id):
        return {"allowed": False, "code": "running"}
    if not await db.scalar(text(_GRADABLE_TURN_SQL), {"sid": session_id}):
        return {"allowed": False, "code": "no_answers"}
    job = (
        (
            await db.execute(
                text("SELECT status, lease_until FROM interview_evaluation_jobs WHERE session_id = :sid"),
                {"sid": session_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if job and str(job["status"]).upper() == "PROCESSING":
        lease = job["lease_until"]
        if lease is not None and lease.tzinfo is None:
            lease = lease.replace(tzinfo=UTC)
        if lease is None or lease > datetime.now(UTC):
            return {"allowed": False, "code": "running"}
    return {"allowed": True, "code": None}


_REFUSALS = {
    "not_closed": (409, "Phiên phải kết thúc trước khi chấm điểm."),
    "already_graded": (409, "Phiên đã có báo cáo; không chấm lại để tránh thay kết quả ứng viên đã xem."),
    "running": (409, "Phiên đang được chấm điểm."),
    "no_answers": (409, "Phiên không có câu chuyên môn nào được trả lời nên không có gì để chấm."),
}


async def _grade(session_id: str) -> bool:
    """Grade one session on its own database session; False when grading failed."""
    try:
        async with SessionFactory() as db:
            await evaluate_closed_session(db, session_id)
        trace_event("interviewer", "evaluation_admin_completed", session_id=session_id)
        return True
    except Exception as exc:  # noqa: BLE001 - reported in the pipeline view
        logger.warning("Admin grading of %s failed: %s", session_id, exc)
        trace_event(
            "interviewer",
            "evaluation_admin_failed",
            session_id=session_id,
            error_type=type(exc).__name__,
            error_message=str(exc)[:300],
        )
        return False
    finally:
        _running.pop(session_id, None)


async def start_grading(db: AsyncSession, session_id: str) -> dict[str, Any]:
    """Start grading in the background; raises ``AdminActionError`` when refused."""
    state = await grading_state(db, session_id)
    if not state["allowed"]:
        status_code, message = _REFUSALS[state["code"]]
        raise AdminActionError(status_code, state["code"], message)
    trace_event("interviewer", "evaluation_admin_requested", session_id=session_id)
    _running[session_id] = asyncio.create_task(_grade(session_id), name=f"admin-grade-{session_id}")
    return {"started": True}


# --------------------------------------------------------------------------- batch

BATCH_LIMIT = 50
_batches: dict[str, dict[str, Any]] = {}

# Closed, no report, no live grading lease, and at least one answered main
# question (a session with only the greeting cannot be graded).
_PENDING_SQL = """
SELECT s.id
FROM interview_sessions s
LEFT JOIN interview_evaluation_jobs j ON j.session_id = s.id
WHERE UPPER(COALESCE(s.status, '')) = 'CLOSED'
  AND NOT EXISTS (SELECT 1 FROM interview_evaluations e WHERE e.session_id = s.id)
  AND NOT (COALESCE(j.status, '') = 'PROCESSING' AND j.lease_until > now())
  AND EXISTS (SELECT 1 FROM interview_turns t WHERE {gradable})
  {user_filter}
ORDER BY s.started_at DESC
LIMIT :limit
"""


def batch_key(user_id: str | None) -> str:
    return f"user:{user_id}" if user_id else "all"


def batch_state(user_id: str | None) -> dict[str, Any] | None:
    state = _batches.get(batch_key(user_id))
    return dict(state) if state else None


def _pending_sql(user_id: str | None) -> str:
    return _PENDING_SQL.format(
        gradable=_GRADABLE_TURN_CONDITION.format(session="s.id"),
        user_filter="AND s.user_id = :uid" if user_id else "",
    )


async def pending_sessions(
    db: AsyncSession, *, user_id: str | None = None, limit: int = BATCH_LIMIT
) -> list[str]:
    params: dict[str, Any] = {"limit": limit}
    if user_id:
        params["uid"] = user_id
    rows = (await db.execute(text(_pending_sql(user_id)), params)).scalars().all()
    return [str(r) for r in rows if not grading_running(str(r))]


async def count_pending(db: AsyncSession, *, user_id: str | None = None) -> int:
    """How many sessions a batch would grade in all (several batches when above the limit).

    This is what the batch button shows: "Chưa có báo cáo" on the list also
    counts sessions that cannot be graded (no answered main question).
    """
    sql = _pending_sql(user_id).split("ORDER BY")[0]
    params: dict[str, Any] = {"uid": user_id} if user_id else {}
    count = await db.scalar(text(f"SELECT COUNT(*) FROM ({sql}) AS pending"), params)
    return int(count or 0)


async def _grade_batch(key: str, session_ids: list[str]) -> None:
    state = _batches[key]
    try:
        for session_id in session_ids:
            if grading_running(session_id):
                state["skipped"] += 1
                continue
            _running[session_id] = asyncio.current_task()  # type: ignore[assignment]
            ok = await _grade(session_id)
            state["graded" if ok else "failed"] += 1
    finally:
        state["running"] = False
        state["finishedAt"] = datetime.now(UTC).isoformat()


async def start_batch_grading(db: AsyncSession, *, user_id: str | None = None) -> dict[str, Any]:
    """Grade, one after another, the closed sessions without a report (at most ``BATCH_LIMIT``)."""
    key = batch_key(user_id)
    current = _batches.get(key)
    if current and current.get("running"):
        raise AdminActionError(409, "batch_running", "Đang chấm hàng loạt; hãy đợi lượt này xong.")
    session_ids = await pending_sessions(db, user_id=user_id)
    if not session_ids:
        raise AdminActionError(409, "nothing_to_grade", "Không có buổi nào cần chấm.")
    _batches[key] = {
        "total": len(session_ids),
        "graded": 0,
        "failed": 0,
        "skipped": 0,
        "running": True,
        "startedAt": datetime.now(UTC).isoformat(),
        "finishedAt": None,
    }
    trace_event("interviewer", "evaluation_admin_batch_requested", sessions=len(session_ids))
    asyncio.create_task(_grade_batch(key, session_ids), name=f"admin-grade-batch-{key}")
    return {"queued": len(session_ids), "limit": BATCH_LIMIT}
