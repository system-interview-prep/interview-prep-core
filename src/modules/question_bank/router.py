"""Admin authoring API and safe active-bank read API."""
# ruff: noqa: E501

from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.roles import QUESTION_AUTHOR, QUESTION_BANK_ADMIN, QUESTION_REVIEWER
from src.core.security import current_user, require_roles
from src.infrastructure.database import get_db
from src.modules.question_bank.import_parser import csv_template, xlsx_template
from src.modules.question_bank.import_service import QuestionImportService
from src.modules.question_bank.schemas import ApproveRequest, CreateQuestionDraftRequest, ReviewRequest
from src.modules.question_bank.service import QuestionBankService

router = APIRouter(prefix="/admin/question-bank", tags=["question-bank"])


def _import_response(import_run: object) -> dict:
    return {
        "importId": str(import_run.id),
        "status": import_run.status,
        "totalRows": import_run.total_rows,
        "validRows": import_run.valid_rows,
        "warningRows": import_run.warning_rows,
        "errorRows": import_run.error_rows,
    }


@router.get("/imports/template")
async def import_template(
    _: dict = Depends(require_roles(QUESTION_AUTHOR, QUESTION_BANK_ADMIN)),
    format: str = Query(default="csv"),
) -> Response:
    if format == "xlsx":
        return Response(
            content=xlsx_template(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=question-bank-import-v1.xlsx"},
        )
    if format != "csv":
        raise HTTPException(422, "format must be csv or xlsx.")
    return Response(
        content=csv_template(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=question-bank-import-v1.csv"},
    )


@router.post("/imports", status_code=status.HTTP_201_CREATED)
async def upload_import(
    file: UploadFile = File(...),
    actor: dict = Depends(require_roles(QUESTION_AUTHOR, QUESTION_BANK_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    import_run = await QuestionImportService(db).create_csv_import(file, actor["sub"])
    await db.commit()
    return _import_response(import_run)


@router.get("/imports/{import_id}")
async def get_import(
    import_id: UUID,
    _: dict = Depends(require_roles(QUESTION_AUTHOR, QUESTION_REVIEWER, QUESTION_BANK_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return _import_response(await QuestionImportService(db).summary(import_id))


@router.get("/imports/{import_id}/rows")
async def get_import_rows(
    import_id: UUID,
    _: dict = Depends(require_roles(QUESTION_AUTHOR, QUESTION_REVIEWER, QUESTION_BANK_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await QuestionImportService(db).rows(import_id)
    return {
        "items": [
            {
                "rowId": str(row.id),
                "rowNumber": row.row_number,
                "status": row.status,
                "payload": row.normalized_payload,
                "errors": row.validation_errors,
                "warnings": row.validation_warnings,
            }
            for row in rows
        ]
    }


@router.patch("/imports/{import_id}/rows/{row_id}")
async def patch_import_row(
    import_id: UUID,
    row_id: UUID,
    payload: dict,
    _: dict = Depends(require_roles(QUESTION_AUTHOR, QUESTION_BANK_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await QuestionImportService(db).patch_row(import_id, row_id, payload)
    await db.commit()
    return {"rowId": str(row.id), "status": row.status, "errors": row.validation_errors}


@router.get("/imports/{import_id}/report")
async def import_report(
    import_id: UUID,
    _: dict = Depends(require_roles(QUESTION_AUTHOR, QUESTION_REVIEWER, QUESTION_BANK_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> Response:
    content = await QuestionImportService(db).report_csv(import_id)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=question-import-{import_id}-report.csv"},
    )


@router.post("/imports/{import_id}/commit")
async def commit_import(
    import_id: UUID,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: dict = Depends(require_roles(QUESTION_BANK_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not idempotency_key:
        raise HTTPException(422, "Idempotency-Key header is required.")
    rows = await QuestionImportService(db).commit(import_id, actor["sub"], idempotency_key)
    await db.commit()
    return {
        "importId": str(import_id),
        "status": "COMMITTED",
        "created": [
            {
                "rowId": str(row.id),
                "questionId": str(row.draft_question_id),
                "questionVersionId": str(row.draft_question_version_id),
            }
            for row in rows
        ],
    }


def _version_response(version: object) -> dict:
    return {
        "questionVersionId": str(version.id),
        "questionId": str(version.question_id),
        "version": version.version,
        "status": version.status,
    }


def _taxonomy_summary(mappings) -> dict:
    taxonomy = {"roles": [], "skills": [], "primaryCompetency": None}
    for mapping in mappings:
        item = {"conceptId": mapping["concept_id"], "relevance": float(mapping["relevance"])}
        if mapping["purpose"] == "TARGET_ROLE":
            taxonomy["roles"].append(item)
        elif mapping["purpose"] == "TARGET_SKILL":
            taxonomy["skills"].append(item)
        elif mapping["purpose"] == "PRIMARY_COMPETENCY":
            taxonomy["primaryCompetency"] = item
    return taxonomy


async def _question_summary(row: dict, taxonomy_mappings=()) -> dict:
    """Build the admin-list read model without exposing runtime-only data."""
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
        "taxonomy": _taxonomy_summary(taxonomy_mappings),
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
        "ORDER BY v.created_at DESC LIMIT 1) qv ON true WHERE " + where
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
    row_items = rows.mappings().all()
    version_ids = [row["question_version_id"] for row in row_items]
    taxonomy_by_version = {version_id: [] for version_id in version_ids}
    if version_ids:
        mappings = await db.execute(
            text(
                "SELECT question_version_id, concept_id, purpose, relevance "
                "FROM question_version_taxonomy_concepts "
                "WHERE question_version_id = ANY(:version_ids) ORDER BY question_version_id, purpose, concept_id"
            ),
            {"version_ids": version_ids},
        )
        for mapping in mappings.mappings():
            taxonomy_by_version[mapping["question_version_id"]].append(mapping)
    return {
        "items": [
            await _question_summary(dict(row), taxonomy_by_version[row["question_version_id"]])
            for row in row_items
        ],
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
    mappings = await db.execute(
        text(
            "SELECT concept_id, purpose, relevance FROM question_version_taxonomy_concepts "
            "WHERE question_version_id = :version_id ORDER BY purpose, concept_id"
        ),
        {"version_id": item["question_version_id"]},
    )
    return await _question_summary(dict(item), mappings.mappings().all())


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
                "currentVersion": None
                if row["version_id"] is None
                else {
                    "rubricVersionId": str(row["version_id"]),
                    "version": row["version"],
                    "status": "APPROVED" if row["approved_at"] else "DRAFT",
                    "criteriaCount": row["criteria_count"],
                    "totalWeight": float(row["total_weight"]),
                },
            }
            for row in rows.mappings().all()
        ]
    }


@router.post("/question-versions/{version_id}/submit")
async def submit(
    version_id: UUID,
    actor: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
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
