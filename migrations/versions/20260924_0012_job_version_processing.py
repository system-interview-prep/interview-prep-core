"""Track the parser lifecycle for JD version drafts."""

from alembic import op

revision = "20260924_0012"
down_revision = "20260923_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE job_description_versions
          ADD COLUMN source_upload_id TEXT REFERENCES job_descriptions(id) ON DELETE SET NULL,
          ADD COLUMN processing_status TEXT NOT NULL DEFAULT 'DONE'
            CHECK (processing_status IN ('PENDING','PROCESSING','DONE','FAILED')),
          ADD COLUMN error TEXT;
        CREATE UNIQUE INDEX ux_job_description_versions_source_upload
          ON job_description_versions(source_upload_id) WHERE source_upload_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX ux_job_description_versions_source_upload")
    op.execute(
        "ALTER TABLE job_description_versions DROP COLUMN error, "
        "DROP COLUMN processing_status, DROP COLUMN source_upload_id"
    )
