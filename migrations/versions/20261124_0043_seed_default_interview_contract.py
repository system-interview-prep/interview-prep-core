"""Persist the current built-in interview contract as the SQL default.

Revision ID: 20261124_0043
Revises: 20261123_0042
"""

from collections.abc import Sequence
import json

import sqlalchemy as sa
from alembic import op

revision: str = "20261124_0043"
down_revision: str | None = "20261123_0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_TYPE_ID = "00000000-0000-0000-0000-00000000b001"
DEFAULT_VERSION_ID = "00000000-0000-0000-0000-00000000b002"
DEFAULT_CONTRACT = {
    "schemaVersion": "1.0",
    "warmUp": {"softAnswerSeconds": 120, "hardAnswerSeconds": 180},
    "validate": {
        "enabled": True,
        "softAnswerSeconds": 180,
        "hardAnswerSeconds": 240,
        "probeEnabled": True,
        "untilRatio": 0.30,
    },
    "technical": {
        "maxQuestions": 8,
        "expectedSecondsPerQuestion": 240,
        "probeEnabled": True,
        "deepDiveUntilRatio": 0.75,
        "challengeUntilRatio": 0.85,
        "challengeForCoding": True,
        "challengeForCvGaps": True,
        "fastFailAfterGiveUps": 2,
    },
    "behavioral": {
        "enabled": True,
        "softAnswerSeconds": 180,
        "hardAnswerSeconds": 240,
        "reserveSeconds": 180,
    },
    "closing": {
        "qnaEnabled": True,
        "maxQuestions": 2,
        "budgetSeconds": 180,
        "reserveSeconds": 60,
        "minSecondsToOpen": 180,
    },
    "pacing": {"emergencyCutoffSeconds": 90, "behindScheduleRatio": 0.8},
    "contract": {
        "objective": "\u0110\u00e1nh gi\u00e1 n\u0103ng l\u1ef1c theo k\u1ebf ho\u1ea1ch c\u00e2u h\u1ecfi \u0111\u00e3 kh\u00f3a.",
        "coverageTarget": "all_frozen_questions",
        "requireBehavioralWhenEnabled": True,
        "maxProbesPerQuestion": 2,
    },
}


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO interview_blueprint_versions
              (id, blueprint_id, version, name, description, status, definition,
               change_summary, based_on_id, created_by, reviewed_by, reviewed_at, activated_at)
            SELECT :version_id, b.id,
                   (SELECT COALESCE(MAX(v.version), 0) + 1 FROM interview_blueprint_versions v),
                   :name, '', 'ACTIVE', CAST(:definition AS jsonb),
                   'Seed SQL default from the current built-in interview contract',
                   NULL, 'system', 'system', now(), now()
            FROM interview_blueprints b
            WHERE b.id = :blueprint_id
              AND NOT EXISTS (
                SELECT 1 FROM interview_blueprint_versions active
                WHERE active.blueprint_id = b.id AND active.status = 'ACTIVE'
              )
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {
            "version_id": DEFAULT_VERSION_ID,
            "blueprint_id": DEFAULT_TYPE_ID,
            "name": "M\u1eb7c \u0111\u1ecbnh hi\u1ec7n t\u1ea1i",
            "definition": json.dumps(DEFAULT_CONTRACT, ensure_ascii=False),
        },
    )
    bind.execute(
        sa.text(
            """
            UPDATE interview_blueprints b
            SET active_version_id = active.id, updated_at = now()
            FROM interview_blueprint_versions active
            WHERE b.id = :blueprint_id
              AND active.blueprint_id = b.id
              AND active.status = 'ACTIVE'
              AND b.active_version_id IS NULL
            """
        ),
        {"blueprint_id": DEFAULT_TYPE_ID},
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE interview_blueprints SET active_version_id = NULL "
            "WHERE id = :blueprint_id AND active_version_id = :version_id"
        ),
        {"blueprint_id": DEFAULT_TYPE_ID, "version_id": DEFAULT_VERSION_ID},
    )
    bind.execute(
        sa.text(
            "DELETE FROM interview_blueprint_versions WHERE id = :version_id AND created_by = 'system'"
        ),
        {"version_id": DEFAULT_VERSION_ID},
    )
