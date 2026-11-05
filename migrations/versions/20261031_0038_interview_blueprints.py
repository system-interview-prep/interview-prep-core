"""Versioned interview blueprints: the stage policy an interview expert configures.

An interview expert (role QUESTION_AUTHOR) drafts a version and submits it; an
admin other than its author activates it (four-eyes rule, as for questions).
At most one version is ACTIVE; activating one retires the previous. A version
is never edited after submission, and every session plan freezes the version
it used (plan_payload.blueprint), so changing the policy never alters a
session already planned. With no ACTIVE version the built-in default applies
(core/interview_blueprint.py).

Revision ID: 20261031_0038
Revises: 20261026_0037
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261031_0038"
down_revision: str | None = "20261026_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS interview_blueprint_versions (
          id VARCHAR(36) PRIMARY KEY,
          version INTEGER NOT NULL UNIQUE,
          name VARCHAR(160) NOT NULL,
          status VARCHAR(16) NOT NULL DEFAULT 'DRAFT'
            CHECK (status IN ('DRAFT', 'IN_REVIEW', 'ACTIVE', 'RETIRED', 'REJECTED')),
          definition JSONB NOT NULL,
          change_summary TEXT NOT NULL DEFAULT '',
          based_on_id VARCHAR(36),
          created_by VARCHAR(36) NOT NULL,
          submitted_at TIMESTAMPTZ,
          reviewed_by VARCHAR(36),
          reviewed_at TIMESTAMPTZ,
          review_note TEXT,
          activated_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_interview_blueprint_versions_active
          ON interview_blueprint_versions(status) WHERE status = 'ACTIVE';
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS interview_blueprint_versions")
