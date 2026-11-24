"""Interview types and their versioned blueprints (migrations 0038, 0039).

Roles:

* Interview expert (QUESTION_AUTHOR): creates a type ("Kỹ thuật nhanh"), or a
  new DRAFT version of an existing one; edits only their own draft; submits it.
* Admin: activates or rejects a version written by someone else (four-eyes
  rule, one step); lists or hides types, orders them, picks the default.
* Candidate: chooses a listed type when starting a session (default
  pre-selected). The session freezes that type's ACTIVE version at planning
  time, so later versions never change it. Demo sessions use the built-in policy.

Each type has at most one ACTIVE version; activating a new one retires the
previous version of the same type. A type with no ACTIVE version runs the
built-in default policy. Only a DRAFT can be edited.

Notifications: admins when a version is submitted; the author when it is
activated or rejected; candidates only when a new type becomes available
(a revision of an existing type does not notify them).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.roles import ADMIN, CANDIDATE
from src.modules.interviews.core.session_policy.blueprint import (
    InterviewBlueprint,
    blueprint_from_payload,
    default_blueprint,
)
from src.modules.notifications.service import notify_role, notify_user

DEFAULT_NAME = "Mặc định hệ thống"
NOTIFICATION_TYPE = "admin.blueprint"
CANDIDATE_NOTIFICATION_TYPE = "interview.type_added"
_COLUMNS = (
    "id, blueprint_id, version, name, description, status, definition, change_summary, based_on_id, "
    "created_by, submitted_at, reviewed_by, reviewed_at, review_note, activated_at, created_at, updated_at"
)
_TYPE_COLUMNS = (
    "id, name, description, is_default, is_listed, display_order, active_version_id, created_by, "
    "created_at, updated_at"
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


def _clean(value: str | None, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _view(row: dict[str, Any], *, actor_id: str, roles: list[str]) -> dict[str, Any]:
    blueprint = blueprint_from_payload(_definition(row))
    own = str(row["created_by"]) == str(actor_id)
    is_admin = ADMIN in roles
    status = row["status"]
    return {
        "id": row["id"],
        "blueprintId": row.get("blueprint_id"),
        "version": row["version"],
        "name": row["name"],
        "description": row.get("description") or "",
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


async def _type_row(db: AsyncSession, blueprint_id: str, *, lock: bool = False) -> dict[str, Any]:
    sql = f"SELECT {_TYPE_COLUMNS} FROM interview_blueprints WHERE id = :id"
    result = await db.execute(text(sql + (" FOR UPDATE" if lock else "")), {"id": blueprint_id})
    row = result.mappings().one_or_none()
    if row is None:
        raise BlueprintError(404, "type_not_found")
    return dict(row)


async def _policy_of(
    db: AsyncSession, type_row: dict[str, Any]
) -> tuple[dict[str, Any] | None, InterviewBlueprint]:
    """The type's ACTIVE version (row, policy); (None, built-in default) when it has none."""
    if not type_row.get("active_version_id"):
        return None, default_blueprint()
    version = await _row(db, type_row["active_version_id"])
    return version, blueprint_from_payload(_definition(version))


async def resolve_blueprint(
    db: AsyncSession, blueprint_id: str | None = None, *, strict: bool = False
) -> tuple[dict[str, Any] | None, InterviewBlueprint]:
    """The reference and policy a new session freezes.

    ``blueprint_id`` is the type the candidate chose; without one, or when it
    is gone (``strict=False``), the default type. ``strict`` raises instead, so
    the candidate is asked to choose again rather than silently switched.
    """
    row = None
    if blueprint_id:
        result = await db.execute(
            text(f"SELECT {_TYPE_COLUMNS} FROM interview_blueprints WHERE id = :id AND is_listed"),
            {"id": blueprint_id},
        )
        row = result.mappings().one_or_none()
        if row is None and strict:
            raise BlueprintError(409, "blueprint_unavailable")
    if row is None:
        result = await db.execute(text(f"SELECT {_TYPE_COLUMNS} FROM interview_blueprints WHERE is_default"))
        row = result.mappings().one_or_none()
    if row is None:
        return None, default_blueprint()
    version, policy = await _policy_of(db, dict(row))
    ref = {
        "blueprintId": row["id"],
        "blueprintName": row["name"],
        "versionId": version["id"] if version else None,
        "version": version["version"] if version else None,
        "name": version["name"] if version else row["name"],
    }
    return ref, policy


async def active_blueprint(db: AsyncSession) -> tuple[dict[str, Any] | None, InterviewBlueprint]:
    """The default type's policy (kept for callers that do not pass a choice)."""
    return await resolve_blueprint(db)


async def _stats(db: AsyncSession) -> dict[str, dict[str, Any]]:
    """Per type: sessions planned, completed share and average score (latest grading)."""
    result = await db.execute(
        text(
            """
            SELECT p.plan_payload->'blueprint'->>'blueprintId' AS blueprint_id,
                   count(*) AS sessions,
                   count(*) FILTER (WHERE s.end_reason = 'COMPLETED') AS completed,
                   avg(e.overall_score) AS average_score
            FROM interview_session_plans p
            JOIN interview_sessions s ON CAST(s.id AS TEXT) = CAST(p.session_id AS TEXT)
            LEFT JOIN LATERAL (
              SELECT overall_score FROM interview_evaluations
              WHERE CAST(session_id AS TEXT) = CAST(s.id AS TEXT)
              ORDER BY evaluated_at DESC NULLS LAST LIMIT 1
            ) e ON true
            WHERE p.plan_payload->'blueprint'->>'blueprintId' IS NOT NULL
            GROUP BY 1
            """
        )
    )
    stats: dict[str, dict[str, Any]] = {}
    for row in result.mappings():
        sessions = int(row["sessions"] or 0)
        completed = int(row["completed"] or 0)
        score = row["average_score"]
        stats[row["blueprint_id"]] = {
            "sessions": sessions,
            "completionRate": round(completed / sessions, 3) if sessions else None,
            "averageScore": round(float(score), 1) if score is not None else None,
        }
    return stats


async def list_types(db: AsyncSession, *, actor_id: str, roles: list[str]) -> dict[str, Any]:
    """Every type with its versions, newest first, and its numbers (admin and experts)."""
    types = [
        dict(row)
        for row in (
            await db.execute(
                text(f"SELECT {_TYPE_COLUMNS} FROM interview_blueprints ORDER BY display_order, created_at")
            )
        ).mappings()
    ]
    versions = [
        dict(row)
        for row in (
            await db.execute(
                text(f"SELECT {_COLUMNS} FROM interview_blueprint_versions ORDER BY version DESC")
            )
        ).mappings()
    ]
    stats = await _stats(db)
    is_admin = ADMIN in roles
    empty = {"sessions": 0, "completionRate": None, "averageScore": None}
    items = []
    for type_row in types:
        own_versions = [v for v in versions if v["blueprint_id"] == type_row["id"]]
        active = next((v for v in own_versions if v["id"] == type_row["active_version_id"]), None)
        policy = blueprint_from_payload(_definition(active)) if active else default_blueprint()
        items.append(
            {
                "id": type_row["id"],
                "name": type_row["name"],
                "description": type_row["description"],
                "isDefault": type_row["is_default"],
                "isListed": type_row["is_listed"],
                "displayOrder": type_row["display_order"],
                "activeVersion": {"id": active["id"], "version": active["version"]} if active else None,
                "preview": policy.preview(),
                "pendingReview": sum(v["status"] == "IN_REVIEW" for v in own_versions),
                "stats": stats.get(type_row["id"], empty),
                "versions": [_view(v, actor_id=actor_id, roles=roles) for v in own_versions],
                "actions": {
                    "newDraft": True,
                    # The default type is always offered; hiding it would leave candidates without one.
                    "toggleListed": is_admin and not type_row["is_default"],
                    "makeDefault": is_admin and type_row["is_listed"] and not type_row["is_default"],
                },
            }
        )
    return {"items": items, "systemDefault": {"name": DEFAULT_NAME, "preview": default_blueprint().preview()}}


async def candidate_types(db: AsyncSession) -> dict[str, Any]:
    """The types a candidate may choose, with what each package gets."""
    rows = (
        await db.execute(
            text(
                f"SELECT {_TYPE_COLUMNS} FROM interview_blueprints WHERE is_listed "
                "ORDER BY is_default DESC, display_order, created_at"
            )
        )
    ).mappings()
    items = []
    for row in rows:
        _, policy = await _policy_of(db, dict(row))
        items.append(
            {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "isDefault": row["is_default"],
                "preview": policy.preview(),
                # Turns every session of this type asks besides technical questions.
                "presetQuestions": 1 + int(policy.validate_cv.enabled) + int(policy.behavioral.enabled),
            }
        )
    return {"items": items}


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


async def _insert_draft(
    db: AsyncSession,
    *,
    blueprint_id: str,
    actor_id: str,
    name: str,
    description: str,
    blueprint: InterviewBlueprint,
    based_on_id: str | None,
    change_summary: str,
) -> str:
    next_version = int(
        await db.scalar(text("SELECT COALESCE(MAX(version), 0) + 1 FROM interview_blueprint_versions")) or 1
    )
    version_id = str(uuid4())
    await db.execute(
        text(
            "INSERT INTO interview_blueprint_versions "
            "(id, blueprint_id, version, name, description, status, definition, change_summary, "
            "based_on_id, created_by) VALUES (:id, :blueprint_id, :version, :name, :description, 'DRAFT', "
            "CAST(:definition AS jsonb), :summary, :based_on, :actor)"
        ),
        {
            "id": version_id,
            "blueprint_id": blueprint_id,
            "version": next_version,
            "name": _clean(name, 160) or f"Phiên bản {next_version}",
            "description": _clean(description, 400),
            "definition": json.dumps(blueprint.to_payload()),
            "summary": change_summary or "",
            "based_on": based_on_id,
            "actor": actor_id,
        },
    )
    return version_id


async def create_type(
    db: AsyncSession, *, actor_id: str, roles: list[str], name: str, description: str = ""
) -> dict[str, Any]:
    """A new, hidden type with its first DRAFT (starting from the default type's policy)."""
    name = _clean(name, 160)
    if not name:
        raise BlueprintError(422, "name_required")
    _, starting_policy = await resolve_blueprint(db)
    blueprint_id = str(uuid4())
    await db.execute(
        text(
            "INSERT INTO interview_blueprints (id, name, description, is_listed, display_order, created_by) "
            "VALUES (:id, :name, :description, false, "
            "(SELECT COALESCE(MAX(display_order), 0) + 1 FROM interview_blueprints), :actor)"
        ),
        {"id": blueprint_id, "name": name, "description": _clean(description, 400), "actor": actor_id},
    )
    version_id = await _insert_draft(
        db,
        blueprint_id=blueprint_id,
        actor_id=actor_id,
        name=name,
        description=description,
        blueprint=starting_policy,
        based_on_id=None,
        change_summary="Tạo kiểu phỏng vấn mới",
    )
    await db.commit()
    return await get_version(db, version_id, actor_id=actor_id, roles=roles)


async def create_draft(
    db: AsyncSession,
    *,
    actor_id: str,
    roles: list[str],
    blueprint_id: str | None = None,
    based_on_id: str | None = None,
    name: str = "",
    definition: Any = None,
    change_summary: str = "",
) -> dict[str, Any]:
    """A new DRAFT of a type, from ``definition``, another version, or the type's ACTIVE one."""
    based_on = await _row(db, based_on_id) if based_on_id else None
    blueprint_id = blueprint_id or (based_on or {}).get("blueprint_id")
    if not blueprint_id:
        default_ref, _ = await resolve_blueprint(db)
        blueprint_id = (default_ref or {}).get("blueprintId")
    if not blueprint_id:
        raise BlueprintError(404, "type_not_found")
    type_row = await _type_row(db, blueprint_id)
    if definition is not None:
        blueprint = _parse(definition)
    elif based_on is not None:
        blueprint = blueprint_from_payload(_definition(based_on))
    else:
        _, blueprint = await _policy_of(db, type_row)
    version_id = await _insert_draft(
        db,
        blueprint_id=blueprint_id,
        actor_id=actor_id,
        name=name or type_row["name"],
        description=type_row["description"],
        blueprint=blueprint,
        based_on_id=based_on_id or type_row.get("active_version_id"),
        change_summary=change_summary,
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
    description: str | None = None,
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
            "UPDATE interview_blueprint_versions SET name = :name, description = :description, "
            "definition = CAST(:definition AS jsonb), change_summary = :summary, updated_at = now() "
            "WHERE id = :id"
        ),
        {
            "id": version_id,
            "name": _clean(name, 160) or row["name"],
            "description": row["description"] if description is None else _clean(description, 400),
            "definition": json.dumps(blueprint.to_payload()),
            "summary": row["change_summary"] if change_summary is None else change_summary,
        },
    )
    await db.commit()
    return await get_version(db, version_id, actor_id=actor_id, roles=roles)


def _link(version_id: str) -> str:
    return f"/admin/interview-blueprints/{version_id}"


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
    # Every admin but the author can activate it (four-eyes rule).
    await notify_role(
        db,
        ADMIN,
        type_=NOTIFICATION_TYPE,
        title="Kiểu phỏng vấn chờ duyệt",
        message=f"{row['name']} (phiên bản {row['version']}) đã được gửi duyệt.",
        data={"kind": "blueprint_submitted", "versionId": version_id, "link": _link(version_id)},
        dedupe_key=f"blueprint:{version_id}:submitted",
        exclude_user_id=str(row["created_by"]),
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
    """Activate (retiring the type's previous ACTIVE version) or reject an IN_REVIEW version."""
    if ADMIN not in roles:
        raise BlueprintError(403, "admin_only")
    row = await _row(db, version_id, lock=True)
    if row["status"] != "IN_REVIEW":
        raise BlueprintError(409, "not_in_review")
    if str(row["created_by"]) == str(actor_id):
        raise BlueprintError(403, "own_blueprint")
    type_row = await _type_row(db, row["blueprint_id"], lock=True)
    now = datetime.now(UTC)
    if activate:
        errors = blueprint_from_payload(_definition(row)).activation_errors()
        if errors:
            raise BlueprintError(422, "does_not_fit", errors)
        await db.execute(
            text(
                "UPDATE interview_blueprint_versions SET status = 'RETIRED', updated_at = now() "
                "WHERE blueprint_id = :blueprint_id AND status = 'ACTIVE'"
            ),
            {"blueprint_id": row["blueprint_id"]},
        )
    elif not (note or "").strip():
        raise BlueprintError(422, "note_required")
    await db.execute(
        text(
            "UPDATE interview_blueprint_versions SET status = :status, reviewed_by = :actor, "
            "reviewed_at = :now, review_note = :note, activated_at = :activated_at, updated_at = now() "
            "WHERE id = :id"
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
    is_new_type = activate and not type_row["active_version_id"]
    if activate:
        # The approved version carries the reviewed name and description shown to candidates.
        # A type's first approval also offers it to candidates.
        await db.execute(
            text(
                "UPDATE interview_blueprints SET active_version_id = :version_id, name = :name, "
                "description = :description, is_listed = is_listed OR :first, updated_at = now() "
                "WHERE id = :id"
            ),
            {
                "id": row["blueprint_id"],
                "version_id": version_id,
                "name": row["name"],
                "description": row["description"],
                "first": is_new_type,
            },
        )
    await _notify_decision(db, row, activate=activate, note=(note or "").strip(), new_type=is_new_type)
    await db.commit()
    return await get_version(db, version_id, actor_id=actor_id, roles=roles)


async def update_type(
    db: AsyncSession,
    blueprint_id: str,
    *,
    roles: list[str],
    is_listed: bool | None = None,
    is_default: bool | None = None,
    display_order: int | None = None,
) -> dict[str, Any]:
    """Admin: show or hide a type, make it the default, or move it in the list."""
    if ADMIN not in roles:
        raise BlueprintError(403, "admin_only")
    row = await _type_row(db, blueprint_id, lock=True)
    listed = row["is_listed"] if is_listed is None else is_listed
    if row["is_default"] and not listed:
        raise BlueprintError(409, "default_must_be_listed")
    if is_default:
        if not listed:
            raise BlueprintError(409, "default_must_be_listed")
        await db.execute(text("UPDATE interview_blueprints SET is_default = false WHERE is_default"))
    await db.execute(
        text(
            "UPDATE interview_blueprints SET is_listed = :listed, is_default = :is_default, "
            "display_order = :display_order, updated_at = now() WHERE id = :id"
        ),
        {
            "id": blueprint_id,
            "listed": listed,
            "is_default": True if is_default else row["is_default"],
            "display_order": row["display_order"] if display_order is None else int(display_order),
        },
    )
    await db.commit()
    updated = await _type_row(db, blueprint_id)
    return {
        "id": updated["id"],
        "isListed": updated["is_listed"],
        "isDefault": updated["is_default"],
        "displayOrder": updated["display_order"],
    }


async def _notify_decision(
    db: AsyncSession, row: dict[str, Any], *, activate: bool, note: str, new_type: bool = False
) -> None:
    """Tell the author the outcome; tell candidates only when a new type becomes available."""
    label = f"{row['name']} (phiên bản {row['version']})"
    await notify_user(
        db,
        str(row["created_by"]),
        type_=NOTIFICATION_TYPE,
        title="Kiểu phỏng vấn đã được duyệt" if activate else "Kiểu phỏng vấn bị từ chối",
        message=f"{label} đang được dùng cho các buổi phỏng vấn mới."
        if activate
        else f"{label} bị từ chối. Lý do: {note}",
        data={
            "kind": "blueprint_activated" if activate else "blueprint_rejected",
            "versionId": row["id"],
            "link": _link(row["id"]),
        },
        dedupe_key=f"blueprint:{row['id']}:{'activated' if activate else 'rejected'}",
    )
    if activate and new_type:
        await notify_role(
            db,
            CANDIDATE,
            type_=CANDIDATE_NOTIFICATION_TYPE,
            title="Có kiểu phỏng vấn mới",
            message=f"Bạn có thể chọn “{row['name']}” khi bắt đầu buổi luyện phỏng vấn.",
            data={
                "kind": "interview_type_added",
                "blueprintId": row["blueprint_id"],
                "link": "/interview/select",
            },
            dedupe_key=f"blueprint-type:{row['blueprint_id']}:added",
        )
