"""Interview types: candidates choose one; each type has versioned blueprints.

0038 kept one ACTIVE version for the whole system, so candidates could not
choose and every revision changed everyone's interview. An interview type
(``interview_blueprints``) is what a candidate picks ("Phỏng vấn đầy đủ",
"Kỹ thuật nhanh"…); its versions are what an expert edits and an admin
approves. Each type has at most one ACTIVE version, which replaces the
previous one on approval. Admins list or hide types, order them and choose
the default (pre-selected for candidates, used by the demo-less shortcuts).

A type with no ACTIVE version runs the built-in default policy. Existing
versions move into the seeded default type "Phỏng vấn đầy đủ".

Revision ID: 20261031_0039
Revises: 20261031_0038
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261031_0039"
down_revision: str | None = "20261031_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_TYPE_ID = "00000000-0000-0000-0000-00000000b001"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS interview_blueprints (
          id VARCHAR(36) PRIMARY KEY,
          name VARCHAR(160) NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          is_default BOOLEAN NOT NULL DEFAULT false,
          is_listed BOOLEAN NOT NULL DEFAULT false,
          display_order INTEGER NOT NULL DEFAULT 100,
          active_version_id VARCHAR(36),
          created_by VARCHAR(36) NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_interview_blueprints_default
          ON interview_blueprints(is_default) WHERE is_default;

        INSERT INTO interview_blueprints
          (id, name, description, is_default, is_listed, display_order, created_by)
        VALUES ('{DEFAULT_TYPE_ID}', 'Phỏng vấn đầy đủ',
                'Đủ các giai đoạn: giới thiệu, dự án trong CV, kỹ thuật, tình huống và hỏi đáp.',
                true, true, 0, 'system')
        ON CONFLICT (id) DO NOTHING;

        ALTER TABLE interview_blueprint_versions
          ADD COLUMN IF NOT EXISTS blueprint_id VARCHAR(36),
          ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';
        UPDATE interview_blueprint_versions SET blueprint_id = '{DEFAULT_TYPE_ID}' WHERE blueprint_id IS NULL;
        ALTER TABLE interview_blueprint_versions ALTER COLUMN blueprint_id SET NOT NULL;
        UPDATE interview_blueprints SET active_version_id = (
          SELECT id FROM interview_blueprint_versions
          WHERE blueprint_id = '{DEFAULT_TYPE_ID}' AND status = 'ACTIVE' LIMIT 1
        ) WHERE id = '{DEFAULT_TYPE_ID}';

        -- One ACTIVE version per type, not per system.
        DROP INDEX IF EXISTS ux_interview_blueprint_versions_active;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_interview_blueprint_versions_active
          ON interview_blueprint_versions(blueprint_id) WHERE status = 'ACTIVE';
        CREATE INDEX IF NOT EXISTS ix_interview_blueprint_versions_type
          ON interview_blueprint_versions(blueprint_id, version DESC);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS ix_interview_blueprint_versions_type;
        DROP INDEX IF EXISTS ux_interview_blueprint_versions_active;
        ALTER TABLE interview_blueprint_versions DROP COLUMN IF EXISTS description;
        ALTER TABLE interview_blueprint_versions DROP COLUMN IF EXISTS blueprint_id;
        DROP TABLE IF EXISTS interview_blueprints;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_interview_blueprint_versions_active
          ON interview_blueprint_versions(status) WHERE status = 'ACTIVE';
        """
    )
