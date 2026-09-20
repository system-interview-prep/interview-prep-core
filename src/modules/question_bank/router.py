"""Admin authoring API and safe active-bank read API."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.roles import QUESTION_AUTHOR, QUESTION_BANK_ADMIN, QUESTION_REVIEWER
from src.core.security import current_user, require_roles
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


async def _question_summary(db: AsyncSession, row: dict) -> dict:
    """Build the admin-list read model without exposing runtime-only data."""
    mappings = await db.execute(
        text(
            "SELECT concept_id, purpose, relevance FROM question_version_taxonomy_concepts "
            "WHERE question_version_id = :version_id ORDER BY purpose, concept_id"
        ),
        {"version_id": row["question_version_id"]},
    )
    taxonomy = {"roles": [], "skills": [], "primaryCompetency": None}
    for mapping in mappings.mappings():
        item = {"conceptId": mapping["concept_id"], "relevance": float(mapping["relevance"])}
        if mapping["purpose"] == "TARGET_ROLE":
            taxonomy["roles"].append(item)
        elif mapping["purpose"] == "TARGET_SKILL":
            taxonomy["skills"].append(item)
        elif mapping["purpose"] == "PRIMARY_COMPETENCY":
            taxonomy["primaryCompetency"] = item
    return {
        "questionId": str(row["question_id"]),
        "stableKey": row["stable_key"],
        "currentVersion": {
            "questionVersionId": str(row["question_version_id"]),
            "version": row["version"],
            "status": row["status"],
            "canonicalText": row["canonical_text"],
            "canonicalLocale": row["canonical_locale"],
            "questionType": row["question_type"],
            "difficultyBand": row["difficulty_band"],
            "softAnswerSeconds": row["soft_answer_seconds"],
        },
        "taxonomy": taxonomy,
        "updatedAt": row["created_at"].isoformat(),
    }


@router.post("/questions/drafts", status_code=status.HTTP_201_CREATED)
async def create_draft(
    payload: CreateQuestionDraftRequest,
    actor: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    version = await QuestionBankService(db).create_draft(payload, actor["sub"])
    await db.commit()
    return _version_response(version)


@router.get("/questions")
async def list_questions(
    _: dict = Depends(require_roles(QUESTION_AUTHOR, QUESTION_REVIEWER, QUESTION_BANK_ADMIN)),
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(default=None, max_length=200),
    status_filter: str | None = Query(default=None, alias="status"),
    difficulty_band: str | None = Query(default=None, alias="difficultyBand"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
) -> dict:
    """Paginated admin read model. A question appears once with its newest version."""
    filters = ["1 = 1"]
    params: dict[str, object] = {"limit": page_size, "offset": (page - 1) * page_size}
    if q:
        filters.append("(q.stable_key ILIKE :q OR qv.canonical_text ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    if status_filter:
        filters.append("qv.status = :status")
        params["status"] = status_filter
    if difficulty_band:
        filters.append("qv.difficulty_band = :difficulty_band")
        params["difficulty_band"] = difficulty_band
    where = " AND ".join(filters)
    base = (
        " FROM interview_questions q JOIN LATERAL ("
        "SELECT * FROM interview_question_versions v WHERE v.question_id = q.id "
        "ORDER BY v.created_at DESC LIMIT 1) qv ON true WHERE "
        + where
    )
    total = await db.scalar(text("SELECT count(*)" + base), params)
    rows = await db.execute(
        text(
            "SELECT q.id AS question_id, q.stable_key, qv.id AS question_version_id, qv.version, "
            "qv.status, qv.canonical_text, qv.canonical_locale, qv.question_type, "
            "qv.difficulty_band, qv.soft_answer_seconds, qv.created_at"
            + base
            + " ORDER BY qv.created_at DESC LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    return {
        "items": [await _question_summary(db, dict(row)) for row in rows.mappings().all()],
        "page": page,
        "pageSize": page_size,
        "total": total or 0,
    }


@router.get("/questions/{question_id}")
async def get_question(
    question_id: UUID,
    _: dict = Depends(require_roles(QUESTION_AUTHOR, QUESTION_REVIEWER, QUESTION_BANK_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.execute(
        text(
            "SELECT q.id AS question_id, q.stable_key, qv.id AS question_version_id, qv.version, "
            "qv.status, qv.canonical_text, qv.canonical_locale, qv.question_type, "
            "qv.difficulty_band, qv.soft_answer_seconds, qv.created_at "
            "FROM interview_questions q JOIN LATERAL (SELECT * FROM interview_question_versions v "
            "WHERE v.question_id = q.id ORDER BY v.created_at DESC LIMIT 1) qv ON true WHERE q.id = :id"
        ),
        {"id": question_id},
    )
    item = row.mappings().one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Question not found.")
    return await _question_summary(db, dict(item))


@router.get("/rubrics")
async def list_rubrics(
    _: dict = Depends(require_roles(QUESTION_AUTHOR, QUESTION_REVIEWER, QUESTION_BANK_ADMIN)),
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(default=None, max_length=200),
) -> dict:
    params: dict[str, object] = {}
    where = ""
    if q:
        where = " WHERE r.stable_key ILIKE :q"
        params["q"] = f"%{q.strip()}%"
    rows = await db.execute(
        text(
            "SELECT r.id, r.stable_key, rv.id AS version_id, rv.version, rv.approved_at, "
            "count(rc.id) AS criteria_count, coalesce(sum(rc.weight), 0) AS total_weight "
            "FROM rubrics r LEFT JOIN rubric_versions rv ON rv.id = r.current_version_id "
            "LEFT JOIN rubric_criteria rc ON rc.rubric_version_id = rv.id"
            + where
            + " GROUP BY r.id, r.stable_key, rv.id, rv.version, rv.approved_at ORDER BY r.stable_key"
        ),
        params,
    )
    return {
        "items": [
            {
                "rubricId": str(row["id"]),
                "stableKey": row["stable_key"],
                "currentVersion": None if row["version_id"] is None else {
                    "rubricVersionId": str(row["version_id"]), "version": row["version"],
                    "status": "APPROVED" if row["approved_at"] else "DRAFT",
                    "criteriaCount": row["criteria_count"], "totalWeight": float(row["total_weight"]),
                },
            }
            for row in rows.mappings().all()
        ]
    }


@router.post("/question-versions/{version_id}/submit")
async def submit(
    version_id: UUID, actor: dict = Depends(require_roles(QUESTION_AUTHOR)), db: AsyncSession = Depends(get_db)
) -> dict:
    version = await QuestionBankService(db).submit(version_id, actor["sub"])
    await db.commit()
    return _version_response(version)


@router.post("/question-versions/{version_id}/reviews", status_code=status.HTTP_201_CREATED)
async def review(
    version_id: UUID,
    payload: ReviewRequest,
    actor: dict = Depends(require_roles(QUESTION_REVIEWER)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    review_record = await QuestionBankService(db).record_review(version_id, actor["sub"], payload)
    await db.commit()
    return {"reviewId": str(review_record.id), "decision": review_record.decision}


@router.post("/question-versions/{version_id}/approve")
async def approve(
    version_id: UUID,
    payload: ApproveRequest,
    actor: dict = Depends(require_roles(QUESTION_BANK_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    version = await QuestionBankService(db).approve(version_id, actor["sub"], payload.approval_policy_version)
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
