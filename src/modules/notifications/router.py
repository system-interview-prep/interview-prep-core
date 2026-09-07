from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _notification(row: dict) -> dict:
    return {
        "id": row["id"],
        "type": row["type"],
        "title": row["title"],
        "message": row["message"],
        "data": row["data"] or {},
        "read": row["read_at"] is not None,
        "readAt": row["read_at"].isoformat() if row["read_at"] else None,
        "createdAt": row["created_at"].isoformat(),
    }


@router.get("")
async def list_notifications(
    unread_only: bool = Query(default=False, alias="unreadOnly"),
    limit: int = Query(default=50, ge=1, le=100),
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    unread_filter = " AND read_at IS NULL" if unread_only else ""
    result = await db.execute(
        text(
            "SELECT id, type, title, message, data, read_at, created_at FROM notifications "
            f"WHERE user_id = :uid{unread_filter} ORDER BY created_at DESC LIMIT :limit"
        ),
        {"uid": user["sub"], "limit": limit},
    )
    return {"items": [_notification(row) for row in result.mappings().all()]}


@router.patch("/{notification_id}/read")
async def mark_read(
    notification_id: str, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    result = await db.execute(
        text(
            "UPDATE notifications SET read_at = COALESCE(read_at, now()) "
            "WHERE id = :id AND user_id = :uid RETURNING id, type, title, message, data, read_at, created_at"
        ),
        {"id": notification_id, "uid": user["sub"]},
    )
    row = result.mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    await db.commit()
    return _notification(row)


@router.delete("/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_notification(
    notification_id: str, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> None:
    result = await db.execute(
        text("DELETE FROM notifications WHERE id = :id AND user_id = :uid"),
        {"id": notification_id, "uid": user["sub"]},
    )
    if result.rowcount == 0:
        await db.rollback()
        raise HTTPException(status_code=404, detail="Notification not found")
    await db.commit()
