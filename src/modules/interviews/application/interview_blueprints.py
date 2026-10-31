"""Versioned interview blueprints (migration 20261031_0038).

Lifecycle, as for questions: an interview expert (QUESTION_AUTHOR) creates a
DRAFT from the active version, edits it and submits it (IN_REVIEW); an admin
other than the author activates it (ACTIVE, the previous one becomes RETIRED)
or rejects it (REJECTED, with a note). Only a DRAFT can be edited, so a
version a session may have used never changes. The planner freezes the
active version into each new plan (``active_blueprint``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.roles import ADMIN
from src.modules.interviews.core.interview_blueprint import (
    InterviewBlueprint,
    blueprint_from_payload,
    default_blueprint,
)

DEFAULT_NAME = "Mặc định hệ thống"
_COLUMNS = (
    "id, version, name, status, definition, change_summary, based_on_id, created_by, submitted_at, "
    "reviewed_by, reviewed_at, review_note, activated_at, created_at, updated_at"
)


class BlueprintError(Exception):
    def __init__(self, status_code: int, code: str, detail: Any = None) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.detail = detail


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


def _definition(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("definition") or {}
    return json.loads(value) if isinstance(value, str) else value


def _view(row: dict[str, Any], *, actor_id: str, roles: list[str]) -> dict[str, Any]:
    blueprint = blueprint_from_payload(_definition(row))
    own = str(row["created_by"]) == str(actor_id)
    is_admin = ADMIN in roles
    status = row["status"]
    return {
        "id": row["id"],
        "version": row["version"],
        "name": row["name"],
        "status": status,
        "definition": blueprint.to_payload(),
        "changeSummary": row["change_summary"],
        "basedOnId": row["based_on_id"],
        "createdBy": row["created_by"],
        "submittedAt": _iso(row["submitted_at"]),
        "reviewedBy": row["reviewed_by"],
        "reviewedAt": _iso(row["reviewed_at"]),
        "reviewNote": row["review_note"],
        "activatedAt": _iso(row["activated_at"]),
        "createdAt": _iso(row["created_at"]),
        "updatedAt": _iso(row["updated_at"]),
        "preview": blueprint.preview(),
        "activationErrors": blueprint.activation_errors(),
        "actions": {
            "edit": status == "DRAFT" and own,
            "submit": status == "DRAFT" and own,
            # Four-eyes rule: the author never activates their own version.
            "activate": status == "IN_REVIEW" and is_admin and not own,
            "reject": status == "IN_REVIEW" and is_admin and not own,
        },
    }


async def _row(db: AsyncSession, version_id: str, *, lock: bool = False) -> dict[str, Any]:
    sql = f"SELECT {_COLUMNS} FROM interview_blueprint_versions WHERE id = :id"
    result = await db.execute(text(sql + (" FOR UPDATE" if lock else "")), {"id": version_id})
    row = result.mappings().one_or_none()
    if row is None:
        raise BlueprintError(404, "blueprint_not_found")
    return dict(row)


async def active_blueprint(db: AsyncSession) -> tuple[dict[str, Any] | None, InterviewBlueprint]:
    """The ACTIVE version's reference and policy; (None, default) when none is active."""
    result = await db.execute(
        text("SELECT id, version, name, definition FROM interview_blueprint_versions WHERE status = 'ACTIVE'")
    )
    row = result.mappings().one_or_none()
    if row is None:
        return None, default_blueprint()
    ref = {"versionId": row["id"], "version": row["version"], "name": row["name"]}
    return ref, blueprint_from_payload(_definition(dict(row)))


async def list_versions(db: AsyncSession, *, actor_id: str, roles: list[str]) -> dict[str, Any]:
    result = await db.execute(
        text(f"SELECT {_COLUMNS} FROM interview_blueprint_versions ORDER BY version DESC")
    )
    items = [_view(dict(row), actor_id=actor_id, roles=roles) for row in result.mappings()]
    default = default_blueprint()
    return {
        "items": items,
        "activeVersionId": next((item["id"] for item in items if item["status"] == "ACTIVE"), None),
        "systemDefault": {
            "name": DEFAULT_NAME,
            "definition": default.to_payload(),
            "preview": default.preview(),
        },
    }


async def get_version(
    db: AsyncSession, version_id: str, *, actor_id: str, roles: list[str]
) -> dict[str, Any]:
    return _view(await _row(db, version_id), actor_id=actor_id, roles=roles)


def _parse(definition: Any) -> InterviewBlueprint:
    try:
        return InterviewBlueprint.model_validate(definition or {})
    except ValidationError as exc:
        errors = [
            {"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
            for error in exc.errors()
        ]
        raise BlueprintError(422, "invalid_blueprint", errors) from exc


async def create_draft(
    db: AsyncSession,
    *,
    actor_id: str,
    roles: list[str],
    name: str,
    based_on_id: str | None = None,
    definition: Any = None,
    change_summary: str = "",
) -> dict[str, Any]:
    """A new DRAFT, starting from ``definition``, another version, or the active one."""
    if definition is not None:
        blueprint = _parse(definition)
    elif based_on_id:
        blueprint = blueprint_from_payload(_definition(await _row(db, based_on_id)))
    else:
        _, blueprint = await active_blueprint(db)
    next_version = int(
        await db.scalar(text("SELECT COALESCE(MAX(version), 0) + 1 FROM interview_blueprint_versions")) or 1
    )
    version_id = str(uuid4())
    await db.execute(
        text(
            "INSERT INTO interview_blueprint_versions "
            "(id, version, name, status, definition, change_summary, based_on_id, created_by) "
            "VALUES (:id, :version, :name, 'DRAFT', CAST(:definition AS jsonb), :summary, :based_on, :actor)"
        ),
        {
            "id": version_id,
            "version": next_version,
            "name": (name or "").strip()[:160] or f"Phiên bản {next_version}",
            "definition": json.dumps(blueprint.to_payload()),
            "summary": change_summary or "",
            "based_on": based_on_id,
            "actor": actor_id,
        },
    )
    await db.commit()
    return await get_version(db, version_id, actor_id=actor_id, roles=roles)


async def update_draft(
    db: AsyncSession,
    version_id: str,
    *,
    actor_id: str,
    roles: list[str],
    name: str | None = None,
    definition: Any = None,
    change_summary: str | None = None,
) -> dict[str, Any]:
    row = await _row(db, version_id, lock=True)
    if row["status"] != "DRAFT":
        raise BlueprintError(409, "not_editable")
    if str(row["created_by"]) != str(actor_id):
        raise BlueprintError(403, "not_author")
    blueprint = _parse(definition) if definition is not None else blueprint_from_payload(_definition(row))
    await db.execute(
        text(
            "UPDATE interview_blueprint_versions SET name = :name, definition = CAST(:definition AS jsonb), "
            "change_summary = :summary, updated_at = now() WHERE id = :id"
        ),
        {
            "id": version_id,
            "name": (name.strip()[:160] if name and name.strip() else row["name"]),
            "definition": json.dumps(blueprint.to_payload()),
            "summary": row["change_summary"] if change_summary is None else change_summary,
        },
    )
    await db.commit()
    return await get_version(db, version_id, actor_id=actor_id, roles=roles)


async def submit(db: AsyncSession, version_id: str, *, actor_id: str, roles: list[str]) -> dict[str, Any]:
    row = await _row(db, version_id, lock=True)
    if row["status"] != "DRAFT":
        raise BlueprintError(409, "not_editable")
    if str(row["created_by"]) != str(actor_id):
        raise BlueprintError(403, "not_author")
    errors = blueprint_from_payload(_definition(row)).activation_errors()
    if errors:
        raise BlueprintError(422, "does_not_fit", errors)
    await db.execute(
        text(
            "UPDATE interview_blueprint_versions SET status = 'IN_REVIEW', submitted_at = now(), "
            "updated_at = now() WHERE id = :id"
        ),
        {"id": version_id},
    )
    await db.commit()
    return await get_version(db, version_id, actor_id=actor_id, roles=roles)


async def decide(
    db: AsyncSession,
    version_id: str,
    *,
    actor_id: str,
    roles: list[str],
    activate: bool,
    note: str = "",
) -> dict[str, Any]:
    """Activate (retiring the previous ACTIVE version) or reject an IN_REVIEW version."""
    if ADMIN not in roles:
        raise BlueprintError(403, "admin_only")
    row = await _row(db, version_id, lock=True)
    if row["status"] != "IN_REVIEW":
        raise BlueprintError(409, "not_in_review")
    if str(row["created_by"]) == str(actor_id):
        raise BlueprintError(403, "own_blueprint")
    now = datetime.now(UTC)
    if activate:
        errors = blueprint_from_payload(_definition(row)).activation_errors()
        if errors:
            raise BlueprintError(422, "does_not_fit", errors)
        await db.execute(
            text(
                "UPDATE interview_blueprint_versions SET status = 'RETIRED', updated_at = now() "
                "WHERE status = 'ACTIVE'"
            )
        )
    elif not (note or "").strip():
        raise BlueprintError(422, "note_required")
    await db.execute(
        text(
            "UPDATE interview_blueprint_versions SET status = :status, reviewed_by = :actor, "
            "reviewed_at = :now, "
            "review_note = :note, activated_at = :activated_at, updated_at = now() WHERE id = :id"
        ),
        {
            "id": version_id,
            "status": "ACTIVE" if activate else "REJECTED",
            "actor": actor_id,
            "now": now,
            "note": (note or "").strip() or None,
            "activated_at": now if activate else None,
        },
    )
    await db.commit()
    return await get_version(db, version_id, actor_id=actor_id, roles=roles)
