"""Admin authoring API and safe active-bank read API."""

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user, require_admin
from src.infrastructure.database import get_db
from src.modules.question_bank.schemas import ApproveRequest, CreateQuestionDraftRequest, ReviewRequest
from src.modules.question_bank.service import QuestionBankService

router = APIRouter(prefix="/admin/question-bank", tags=["question-bank"])


def _version_response(version: object) -> dict:
    return {
        "questionVersionId": str(version.id),
        "questionId": str(version.question_id),
        "version": version.version,
        "status": version.status,
    }


@router.post("/questions/drafts", status_code=status.HTTP_201_CREATED)
async def create_draft(
    payload: CreateQuestionDraftRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    version = await QuestionBankService(db).create_draft(payload, admin["sub"])
    await db.commit()
    return _version_response(version)


@router.post("/question-versions/{version_id}/submit")
async def submit(
    version_id: UUID, admin: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    version = await QuestionBankService(db).submit(version_id, admin["sub"])
    await db.commit()
    return _version_response(version)


@router.post("/question-versions/{version_id}/reviews", status_code=status.HTTP_201_CREATED)
async def review(
    version_id: UUID,
    payload: ReviewRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    review_record = await QuestionBankService(db).record_review(version_id, admin["sub"], payload)
    await db.commit()
    return {"reviewId": str(review_record.id), "decision": review_record.decision}


@router.post("/question-versions/{version_id}/approve")
async def approve(
    version_id: UUID,
    payload: ApproveRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    version = await QuestionBankService(db).approve(version_id, admin["sub"], payload.approval_policy_version)
    await db.commit()
    return _version_response(version)


@router.get("/active")
async def list_active_questions(
    _: dict = Depends(current_user), db: AsyncSession = Depends(get_db), limit: int = 50
) -> dict:
    result = await db.execute(
        text("SELECT * FROM active_question_bank ORDER BY stable_key LIMIT :limit"),
        {"limit": min(max(limit, 1), 200)},
    )
    return {"items": [dict(row) for row in result.mappings().all()]}
