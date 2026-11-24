from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import require_admin
from src.infrastructure.database import get_db
from src.modules.admin.processing import cv_parsing_runs, matching_runs
from src.modules.admin.shared.audit import log_admin_action as _log_admin_action
from src.modules.user_cvs.facade import CvAdminReparseError, reparse_cv_as_admin

router = APIRouter(tags=["admin"])

@router.get("/matching/runs")
async def list_admin_matching_runs(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: Literal["all", "running", "completed", "failed"] = Query("all"),
    days: int = Query(7, ge=1, le=90),
    q: str | None = Query(None, max_length=120),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """CV-JD matching runs, newest first (polled while runs are in progress)."""
    try:
        return await matching_runs.list_runs(db, limit=limit, offset=offset, status=status, days=days, q=q)
    except SQLAlchemyError as exc:  # table missing until migration 0032 is applied
        await db.rollback()
        raise HTTPException(status_code=503, detail=matching_runs.NOT_MIGRATED) from exc

@router.get("/matching/runs/{run_id}")
async def get_admin_matching_run(
    run_id: str,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One matching run step by step: requirements, scoring, clarification, AI calls."""
    try:
        detail = await matching_runs.load_run_detail(db, run_id[:36])
    except SQLAlchemyError as exc:  # table missing until migration 0032 is applied
        await db.rollback()
        raise HTTPException(status_code=503, detail=matching_runs.NOT_MIGRATED) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="Matching run not found")
    return detail

@router.get("/cv-parsing/runs")
async def list_admin_cv_parsing_runs(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: Literal["all", "running", "completed", "review", "failed"] = Query("all"),
    days: int = Query(7, ge=1, le=90),
    q: str | None = Query(None, max_length=120),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Uploaded CVs and how far their parsing got, newest first (polled while CVs are parsing)."""
    try:
        return await cv_parsing_runs.list_cv_runs(
            db, limit=limit, offset=offset, status=status, days=days, q=q
        )
    except SQLAlchemyError as exc:  # tables missing until migration 0033 is applied
        await db.rollback()
        raise HTTPException(status_code=503, detail=cv_parsing_runs.NOT_MIGRATED) from exc

@router.get("/cv-parsing/runs/{cv_id}")
async def get_admin_cv_parsing_run(
    cv_id: str,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One CV step by step: upload, queue, extraction, parse, quality gate, save, AI calls."""
    try:
        detail = await cv_parsing_runs.load_cv_detail(db, cv_id[:36])
    except SQLAlchemyError as exc:  # tables missing until migration 0033 is applied
        await db.rollback()
        raise HTTPException(status_code=503, detail=cv_parsing_runs.NOT_MIGRATED) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="CV not found")
    return detail

@router.post("/cv-parsing/runs/{cv_id}/reparse", status_code=202)
async def reparse_admin_cv(
    cv_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Parse a user's CV again (failed, stuck, or to pick up a parser fix)."""
    try:
        result = await reparse_cv_as_admin(db, cv_id[:36])
    except CvAdminReparseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    await _log_admin_action(
        db, admin, "REPARSE_CV", target_id=cv_id[:36], detail={"previousStatus": result["previousStatus"]}
    )
    return {"success": True, **result}
