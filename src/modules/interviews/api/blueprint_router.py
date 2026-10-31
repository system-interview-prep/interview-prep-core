"""Admin API for versioned interview blueprints (application/interview_blueprints.py).

Interview experts (QUESTION_AUTHOR) read, draft, edit and submit versions; an
admin other than the author activates or rejects them.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.roles import QUESTION_AUTHOR
from src.core.security import require_admin, require_roles
from src.infrastructure.database import get_db
from src.modules.interviews.application import interview_blueprints as blueprints
from src.modules.interviews.core.interview_blueprint import InterviewBlueprint

router = APIRouter(prefix="/admin/interview-blueprints", tags=["interview-blueprints"])


class _Body(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class CreateBlueprintRequest(_Body):
    name: str = Field("", max_length=160)
    based_on_id: str | None = Field(None, alias="basedOnId")
    definition: dict[str, Any] | None = None
    change_summary: str = Field("", alias="changeSummary", max_length=2000)


class UpdateBlueprintRequest(_Body):
    name: str | None = Field(None, max_length=160)
    definition: dict[str, Any] | None = None
    change_summary: str | None = Field(None, alias="changeSummary", max_length=2000)


class DecisionRequest(_Body):
    note: str = Field("", max_length=2000)


def _actor(user: dict) -> dict[str, Any]:
    return {"actor_id": str(user["sub"]), "roles": list(user.get("roles", []))}


def _http(exc: blueprints.BlueprintError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"errorCode": exc.code, "message": exc.code, "details": exc.detail},
    )


@router.get("")
async def list_blueprints(
    user: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await blueprints.list_versions(db, **_actor(user))


@router.post("/preview")
async def preview_blueprint(
    definition: dict[str, Any],
    _: dict = Depends(require_roles(QUESTION_AUTHOR)),
) -> dict:
    """Validate an unsaved definition and show what each session package would get."""
    try:
        blueprint = blueprints._parse(definition)
    except blueprints.BlueprintError as exc:
        raise _http(exc) from exc
    return {"preview": blueprint.preview(), "activationErrors": blueprint.activation_errors()}


@router.get("/default")
async def default_definition(_: dict = Depends(require_roles(QUESTION_AUTHOR))) -> dict:
    blueprint = InterviewBlueprint()
    return {"definition": blueprint.to_payload(), "preview": blueprint.preview()}


@router.get("/{version_id}")
async def get_blueprint(
    version_id: str,
    user: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await blueprints.get_version(db, version_id, **_actor(user))
    except blueprints.BlueprintError as exc:
        raise _http(exc) from exc


@router.post("", status_code=201)
async def create_blueprint(
    payload: CreateBlueprintRequest,
    user: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await blueprints.create_draft(
            db,
            **_actor(user),
            name=payload.name,
            based_on_id=payload.based_on_id,
            definition=payload.definition,
            change_summary=payload.change_summary,
        )
    except blueprints.BlueprintError as exc:
        raise _http(exc) from exc


@router.patch("/{version_id}")
async def update_blueprint(
    version_id: str,
    payload: UpdateBlueprintRequest,
    user: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await blueprints.update_draft(
            db,
            version_id,
            **_actor(user),
            name=payload.name,
            definition=payload.definition,
            change_summary=payload.change_summary,
        )
    except blueprints.BlueprintError as exc:
        raise _http(exc) from exc


@router.post("/{version_id}/submit")
async def submit_blueprint(
    version_id: str,
    user: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await blueprints.submit(db, version_id, **_actor(user))
    except blueprints.BlueprintError as exc:
        raise _http(exc) from exc


@router.post("/{version_id}/activate")
async def activate_blueprint(
    version_id: str,
    payload: DecisionRequest | None = None,
    user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await blueprints.decide(
            db, version_id, **_actor(user), activate=True, note=payload.note if payload else ""
        )
    except blueprints.BlueprintError as exc:
        raise _http(exc) from exc


@router.post("/{version_id}/reject")
async def reject_blueprint(
    version_id: str,
    payload: DecisionRequest,
    user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await blueprints.decide(db, version_id, **_actor(user), activate=False, note=payload.note)
    except blueprints.BlueprintError as exc:
        raise _http(exc) from exc
