"""API for interview types and their versioned blueprints (application/interview_blueprints.py).

Admin console: interview experts (QUESTION_AUTHOR) list types, create a type
or a draft, edit and submit their drafts; an admin other than the author
activates or rejects a version, and lists, hides, orders or makes a type the
default. Candidates read the listed types when they start a session.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.roles import QUESTION_AUTHOR
from src.core.security import current_user, require_admin, require_roles
from src.infrastructure.database import get_db
from src.modules.interviews.application import interview_blueprints as blueprints
from src.modules.interviews.core.interview_blueprint import InterviewBlueprint

router = APIRouter(prefix="/admin/interview-blueprints", tags=["interview-blueprints"])
candidate_router = APIRouter(prefix="/interview-types", tags=["interview-blueprints"])


class _Body(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class CreateTypeRequest(_Body):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field("", max_length=400)


class UpdateTypeRequest(_Body):
    is_listed: bool | None = Field(None, alias="isListed")
    is_default: bool | None = Field(None, alias="isDefault")
    display_order: int | None = Field(None, alias="displayOrder", ge=0, le=10000)


class CreateBlueprintRequest(_Body):
    blueprint_id: str | None = Field(None, alias="blueprintId")
    based_on_id: str | None = Field(None, alias="basedOnId")
    name: str = Field("", max_length=160)
    definition: dict[str, Any] | None = None
    change_summary: str = Field("", alias="changeSummary", max_length=2000)


class UpdateBlueprintRequest(_Body):
    name: str | None = Field(None, max_length=160)
    description: str | None = Field(None, max_length=400)
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


@candidate_router.get("")
async def list_interview_types(
    _: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The interview types a signed-in candidate may choose."""
    return await blueprints.candidate_types(db)


@router.get("")
async def list_blueprints(
    user: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await blueprints.list_types(db, **_actor(user))


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


@router.post("/types", status_code=201)
async def create_type(
    payload: CreateTypeRequest,
    user: dict = Depends(require_roles(QUESTION_AUTHOR)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A new interview type, hidden until its first version is activated; returns its draft."""
    try:
        return await blueprints.create_type(
            db, **_actor(user), name=payload.name, description=payload.description
        )
    except blueprints.BlueprintError as exc:
        raise _http(exc) from exc


@router.patch("/types/{blueprint_id}")
async def update_type(
    blueprint_id: str,
    payload: UpdateTypeRequest,
    user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await blueprints.update_type(
            db,
            blueprint_id,
            roles=list(user.get("roles", [])),
            is_listed=payload.is_listed,
            is_default=payload.is_default,
            display_order=payload.display_order,
        )
    except blueprints.BlueprintError as exc:
        raise _http(exc) from exc


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
    """A new draft of a type (from its active version, or from ``basedOnId``)."""
    try:
        return await blueprints.create_draft(
            db,
            **_actor(user),
            blueprint_id=payload.blueprint_id,
            based_on_id=payload.based_on_id,
            name=payload.name,
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
            description=payload.description,
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
