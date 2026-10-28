"""Admin authoring API and safe active-bank read API."""
# ruff: noqa: E501

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.admin_audit import record_admin_action
from src.core.roles import QUESTION_AUTHOR
from src.core.security import require_admin, require_roles
from src.infrastructure.database import get_db
from src.modules.question_bank import insights
from src.modules.question_bank.facade import GENERATED_QUESTION_AUTHOR
from src.modules.question_bank.import_parser import csv_template, xlsx_template
from src.modules.question_bank.import_service import QuestionImportService
from src.modules.question_bank.permissions import (
    REVIEW_TYPES,
    version_actions,
    work_buckets_for,
    work_filter,
)
from src.modules.question_bank.schemas import (
    ApproveRequest,
    AttachRubricRequest,
    CreateQuestionDraftRequest,
    ReviewRequest,
    UpdateQuestionDraftRequest,
)
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
    _: dict = Depends(require_roles(QUESTION_AUTHOR)),
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
    actor: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    import_run = await QuestionImportService(db).create_csv_import(file, actor["sub"])
    await db.commit()
    return _import_response(import_run)


@router.get("/imports/{import_id}")
async def get_import(
    import_id: UUID,
    _: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return _import_response(await QuestionImportService(db).summary(import_id))


@router.get("/imports/{import_id}/rows")
async def get_import_rows(
    import_id: UUID,
    _: dict = Depends(require_roles(QUESTION_AUTHOR)),
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
    _: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await QuestionImportService(db).patch_row(import_id, row_id, payload)
    await db.commit()
    return {"rowId": str(row.id), "status": row.status, "errors": row.validation_errors}


@router.get("/imports/{import_id}/report")
async def import_report(
    import_id: UUID,
    _: dict = Depends(require_roles(QUESTION_AUTHOR)),
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
    actor: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not idempotency_key:
        raise HTTPException(422, "Idempotency-Key header is required.")
    rows = await QuestionImportService(db).commit(import_id, actor["sub"], idempotency_key)
    await db.commit()
    await record_admin_action(db, actor, "IMPORT_QUESTIONS", target_id=import_id, detail={"created": len(rows)})
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
    created_by = row.get("created_by")
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
            # Drafts the selector generated for a skill the bank lacked (question_generation).
            "generated": created_by == GENERATED_QUESTION_AUTHOR,
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
    await record_admin_action(db, actor, "CREATE_QUESTION", target_id=version.id)
    return _version_response(version)


@router.get("/questions")
async def list_questions(
    actor: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(default=None, max_length=200),
    status_filter: str | None = Query(default=None, alias="status"),
    difficulty_band: str | None = Query(default=None, alias="difficultyBand"),
    mine: Literal["drafts", "changes", "review", "approve"] | None = Query(default=None),
    source: Literal["ai", "human"] | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
) -> dict:
    """Paginated admin read model. A question appears once with its newest version.

    ``mine`` narrows to the signed-in person's work (permissions.work_filter);
    ``source=ai`` to drafts the system generated.
    """
    filters = ["1 = 1"]
    params: dict[str, object] = {"limit": page_size, "offset": (page - 1) * page_size}
    work = work_filter(mine)
    if work:
        filters.append(work)
        params["me"] = str(actor["sub"])
    if source:
        filters.append("qv.created_by " + ("=" if source == "ai" else "<>") + " :generator")
        params["generator"] = GENERATED_QUESTION_AUTHOR
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
            "qv.difficulty_band, qv.soft_answer_seconds, qv.created_at, qv.created_by"
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


@router.get("/questions/work-summary")
async def work_summary(
    actor: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """How many questions wait for the signed-in person, per "my work" bucket their roles have."""
    base = (
        "SELECT count(*) FROM interview_questions q JOIN LATERAL ("
        "SELECT * FROM interview_question_versions v WHERE v.question_id = q.id "
        "ORDER BY v.created_at DESC LIMIT 1) qv ON true WHERE q.retired_at IS NULL AND "
    )
    counts: dict[str, int] = {}
    for bucket in work_buckets_for(actor.get("roles") or []):
        counts[bucket] = int(await db.scalar(text(base + work_filter(bucket)), {"me": str(actor["sub"])}) or 0)
    generated = await db.scalar(
        text(base + "qv.created_by = :generator AND qv.status IN ('IN_REVIEW', 'PILOT_READY')"),
        {"generator": GENERATED_QUESTION_AUTHOR},
    )
    return {"buckets": counts, "generatedInReview": int(generated or 0), "roles": actor.get("roles") or []}


async def _reviews(db: AsyncSession, version_id: object) -> list[dict]:
    rows = await db.execute(
        text(
            "SELECT r.id, r.reviewer_id, r.review_type, r.decision, r.comment, r.reviewed_at, "
            "u.name AS reviewer_name, u.email AS reviewer_email "
            "FROM question_reviews r LEFT JOIN users u ON CAST(u.id AS TEXT) = r.reviewer_id "
            "WHERE r.question_version_id = :version_id ORDER BY r.reviewed_at"
        ),
        {"version_id": version_id},
    )
    return [
        {
            "id": str(row["id"]),
            "reviewerId": row["reviewer_id"],
            "reviewer": row["reviewer_name"] or row["reviewer_email"],
            "reviewType": row["review_type"],
            "decision": row["decision"],
            "comment": row["comment"],
            "reviewedAt": row["reviewed_at"].isoformat() if row["reviewed_at"] else None,
        }
        for row in rows.mappings().all()
    ]


async def _person(db: AsyncSession, user_id: str | None) -> str | None:
    if not user_id or user_id == GENERATED_QUESTION_AUTHOR:
        return None
    row = (
        await db.execute(
            text("SELECT name, email FROM users WHERE CAST(id AS TEXT) = :id"), {"id": user_id}
        )
    ).mappings().one_or_none()
    return (row["name"] or row["email"]) if row else None


@router.get("/questions/{question_id}")
async def get_question(
    question_id: UUID,
    actor: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.execute(
        text(
            "SELECT q.id AS question_id, q.stable_key, qv.id AS question_version_id, qv.version, "
            "qv.status, qv.canonical_text, qv.canonical_locale, qv.question_type, "
            "qv.difficulty_band, qv.soft_answer_seconds, qv.created_at, qv.objective, qv.created_by, "
            "qv.thinking_seconds, qv.hard_answer_seconds, q.current_approved_version_id "
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
    summary = await _question_summary(dict(item), mappings.mappings().all())
    summary["currentVersion"]["objective"] = item["objective"]
    summary["currentVersion"]["createdBy"] = item["created_by"]
    summary["currentVersion"]["thinkingSeconds"] = item["thinking_seconds"]
    summary["currentVersion"]["hardAnswerSeconds"] = item["hard_answer_seconds"]
    approved_id = item["current_approved_version_id"]
    summary["approvedVersionId"] = str(approved_id) if approved_id else None
    summary["rubric"] = await _version_rubric(db, item["question_version_id"])
    reviews = await _reviews(db, item["question_version_id"])
    summary["reviews"] = reviews
    summary["currentVersion"]["createdByName"] = await _person(db, item["created_by"])
    summary["actions"] = version_actions(
        status=item["status"],
        created_by=item["created_by"],
        actor_id=str(actor["sub"]),
        roles=actor.get("roles") or [],
        has_rubric=summary["rubric"] is not None,
        approve_reviews=sum(1 for r in reviews if r["decision"] == "APPROVE"),
    )
    summary["reviewTypes"] = list(REVIEW_TYPES)
    return summary


async def _version_rubric(db: AsyncSession, version_id: object) -> dict | None:
    link = await db.execute(
        text(
            "SELECT rv.id, rv.version, rv.minimum_coverage, rv.approved_at, r.stable_key "
            "FROM question_version_rubrics qvr JOIN rubric_versions rv ON rv.id = qvr.rubric_version_id "
            "JOIN rubrics r ON r.id = rv.rubric_id WHERE qvr.question_version_id = :version_id"
        ),
        {"version_id": version_id},
    )
    rubric = link.mappings().one_or_none()
    if rubric is None:
        return None
    criteria = await db.execute(
        text(
            "SELECT stable_key, name, description, weight, critical FROM rubric_criteria "
            "WHERE rubric_version_id = :rubric_version_id ORDER BY display_order"
        ),
        {"rubric_version_id": rubric["id"]},
    )
    return {
        "rubricVersionId": str(rubric["id"]),
        "stableKey": rubric["stable_key"],
        "version": rubric["version"],
        "minimumCoverage": float(rubric["minimum_coverage"]),
        "approved": rubric["approved_at"] is not None,
        "criteria": [
            {
                "stableKey": row["stable_key"],
                "name": row["name"],
                "description": row["description"],
                "weight": float(row["weight"]),
                "critical": row["critical"],
            }
            for row in criteria.mappings().all()
        ],
    }


@router.get("/rubrics")
async def list_rubrics(
    _: dict = Depends(require_roles(QUESTION_AUTHOR)),
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
            "count(rc.id) AS criteria_count, coalesce(sum(rc.weight), 0) AS total_weight, "
            "owner.question_id, owner.question_stable_key, owner.question_version "
            "FROM rubrics r LEFT JOIN rubric_versions rv ON rv.id = r.current_version_id "
            "LEFT JOIN rubric_criteria rc ON rc.rubric_version_id = rv.id "
            # Rubrics are authored on a question version (key `<question>@<version>`)
            # and may be reused by imports; the owner is the question whose key
            # matches, else the earliest linked question version.
            "LEFT JOIN LATERAL (SELECT qv.question_id, q.stable_key AS question_stable_key, "
            "qv.version AS question_version FROM rubric_versions orv "
            "JOIN question_version_rubrics qvr ON qvr.rubric_version_id = orv.id "
            "JOIN interview_question_versions qv ON qv.id = qvr.question_version_id "
            "JOIN interview_questions q ON q.id = qv.question_id "
            "WHERE orv.rubric_id = r.id "
            "ORDER BY (q.stable_key || '@' || qv.version = r.stable_key) DESC, qv.created_at "
            "LIMIT 1) owner ON true"
            + where
            + " GROUP BY r.id, r.stable_key, rv.id, rv.version, rv.approved_at, "
            "owner.question_id, owner.question_stable_key, owner.question_version ORDER BY r.stable_key"
        ),
        params,
    )
    return {
        "items": [
            {
                "rubricId": str(row["id"]),
                "stableKey": row["stable_key"],
                "questionId": None if row["question_id"] is None else str(row["question_id"]),
                "questionStableKey": row["question_stable_key"],
                "questionVersion": row["question_version"],
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


@router.post("/question-versions/{version_id}/revise", status_code=status.HTTP_201_CREATED)
async def revise_version(
    version_id: UUID,
    actor: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    version = await QuestionBankService(db).revise(version_id, actor["sub"])
    await db.commit()
    await record_admin_action(
        db, actor, "REVISE_QUESTION", target_id=version.id, detail={"fromVersionId": str(version_id)}
    )
    return _version_response(version)


@router.patch("/question-versions/{version_id}")
async def update_draft(
    version_id: UUID,
    payload: UpdateQuestionDraftRequest,
    actor: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    version = await QuestionBankService(db).update_draft(version_id, actor["sub"], payload)
    await db.commit()
    await record_admin_action(
        db, actor, "EDIT_QUESTION", target_id=version_id, detail={"fields": sorted(payload.model_fields_set)}
    )
    return _version_response(version)


@router.put("/question-versions/{version_id}/rubric")
async def attach_rubric(
    version_id: UUID,
    payload: AttachRubricRequest,
    actor: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rubric_version = await QuestionBankService(db).attach_rubric(version_id, actor["sub"], payload)
    await db.commit()
    await record_admin_action(
        db, actor, "EDIT_RUBRIC", target_id=version_id, detail={"rubricVersionId": str(rubric_version.id)}
    )
    return {"questionVersionId": str(version_id), "rubricVersionId": str(rubric_version.id)}


@router.post("/question-versions/{version_id}/submit")
async def submit(
    version_id: UUID,
    actor: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    version = await QuestionBankService(db).submit(version_id, actor["sub"])
    await db.commit()
    await record_admin_action(db, actor, "SUBMIT_QUESTION", target_id=version_id)
    return _version_response(version)


@router.post("/question-versions/{version_id}/reviews", status_code=status.HTTP_201_CREATED)
async def review(
    version_id: UUID,
    payload: ReviewRequest,
    actor: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    review_record = await QuestionBankService(db).record_review(version_id, actor["sub"], payload)
    await db.commit()
    await record_admin_action(
        db, actor, "REVIEW_QUESTION", target_id=version_id, detail={"decision": review_record.decision}
    )
    return {"reviewId": str(review_record.id), "decision": review_record.decision}


@router.post("/question-versions/{version_id}/approve")
async def approve(
    version_id: UUID,
    payload: ApproveRequest,
    actor: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    version = await QuestionBankService(db).approve(version_id, actor["sub"], payload.approval_policy_version)
    await db.commit()
    await record_admin_action(db, actor, "APPROVE_QUESTION", target_id=version_id)
    return _version_response(version)


@router.get("/active")
async def list_active_questions(
    # Returns canonical text and the scoring objective of every approved
    # question; a candidate must never be able to read the bank before an
    # interview.
    _: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
) -> dict:
    result = await db.execute(
        text("SELECT * FROM active_question_bank ORDER BY stable_key LIMIT :limit"),
        {"limit": min(max(limit, 1), 200)},
    )
    return {"items": [dict(row) for row in result.mappings().all()]}


@router.get("/coverage")
async def question_coverage(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """How well the bank covers locked sessions and the ACTIVE jobs' must-have skills."""
    from src.modules.interviews.facade import question_coverage_report

    return await question_coverage_report(db)


# --------------------------------------------------------------------------- suggestions from real interviews


class InsightsRunRequest(BaseModel):
    withAi: bool = True


class SuggestionDecision(BaseModel):
    decision: Literal["ACCEPT", "DISMISS"]
    note: str | None = Field(default=None, max_length=500)


@router.get("/insights")
async def insights_state(
    _: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The last analysis run and how many suggestions are open, per kind."""
    return {"run": insights.run_state(), "open": await insights.counts(db)}


@router.post("/insights/run", status_code=status.HTTP_202_ACCEPTED)
async def run_insights(
    payload: InsightsRunRequest | None = None,
    actor: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Analyse real answers to bank questions in the background (up to 5 LLM calls with ``withAi``)."""
    with_ai = payload.withAi if payload else True
    state = insights.start_run(with_ai=with_ai)
    await record_admin_action(db, actor, "RUN_BANK_INSIGHTS", detail={"withAi": with_ai})
    return {"run": state}


@router.get("/suggestions")
async def list_bank_suggestions(
    _: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
    status_filter: Literal["OPEN", "ACCEPTED", "DISMISSED"] = Query(default="OPEN", alias="status"),
    question_id: UUID | None = Query(default=None, alias="questionId"),
    kind: str | None = Query(default=None, max_length=24),
) -> dict:
    return {"items": await insights.list_suggestions(db, status=status_filter, question_id=question_id, kind=kind)}


@router.post("/suggestions/{suggestion_id}/decision")
async def decide_bank_suggestion(
    suggestion_id: UUID,
    payload: SuggestionDecision,
    actor: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Accept (revision draft, AI variants, retirement) or dismiss one suggestion."""
    outcome = await insights.decide(db, suggestion_id, actor, payload.decision, payload.note)
    await record_admin_action(
        db,
        actor,
        "DECIDE_BANK_SUGGESTION",
        target_id=suggestion_id,
        detail={"decision": payload.decision, **outcome["result"], "note": payload.note},
    )
    return outcome


@router.post("/coverage/prepare", status_code=status.HTTP_202_ACCEPTED)
async def prepare_question_coverage(
    _: dict = Depends(require_admin),
) -> dict:
    """Backfill: queue gated pre-generation for every ACTIVE job (idempotent)."""
    from src.modules.interviews.facade import enqueue_question_coverage

    return {"queued": enqueue_question_coverage()}
