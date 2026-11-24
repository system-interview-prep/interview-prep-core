"""Re-run the parse of any user's CV from the admin "Phân tích CV" page.

Same path as the owner's "Phân tích lại" button (``CvReviewService.reparse``):
the row goes back to PENDING and the worker is asked again. Accepted for a
finished or failed parse, and for one stuck in PARSING for over 15 minutes
(the repository's own rule); anything else is still in flight.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.user_cvs.application.review_service import (
    CvReviewConflictError,
    CvReviewNotFoundError,
    CvReviewService,
)
from src.modules.user_cvs.infrastructure.messaging.celery_publisher import CeleryCvParsePublisher
from src.modules.user_cvs.infrastructure.repositories.cv_repository import SqlAlchemyCvRepository


class CvAdminReparseError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


async def reparse_cv_as_admin(db: AsyncSession, cv_id: str) -> dict[str, Any]:
    row = (
        (
            await db.execute(
                text(
                    "SELECT user_id, status, (updated_at < now() - interval '15 minutes') AS stale "
                    "FROM user_cvs WHERE id = :id"
                ),
                {"id": cv_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise CvAdminReparseError(404, "not_found", "Không tìm thấy CV.")
    status = str(row["status"] or "").upper()
    if status not in {"DONE", "FAILED"} and not (status == "PARSING" and row["stale"]):
        raise CvAdminReparseError(
            409, "in_progress", "CV đang được phân tích; hãy đợi lượt hiện tại kết thúc."
        )
    service = CvReviewService(SqlAlchemyCvRepository(db), CeleryCvParsePublisher())
    try:
        await service.reparse(str(row["user_id"]), cv_id)
    except CvReviewNotFoundError as exc:
        raise CvAdminReparseError(409, "in_progress", "CV vừa đổi trạng thái; hãy tải lại trang.") from exc
    except CvReviewConflictError as exc:
        raise CvAdminReparseError(503, "queue_unavailable", "Hàng đợi phân tích CV không hoạt động.") from exc
    return {"cvId": cv_id, "previousStatus": status, "status": "PENDING"}
