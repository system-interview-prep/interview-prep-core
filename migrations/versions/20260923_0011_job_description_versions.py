"""Version immutable JD content while retaining a stable job identity."""

from alembic import op

revision = "20260923_0011"
down_revision = "20260922_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE job_description_versions (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          job_description_id TEXT NOT NULL REFERENCES job_descriptions(id) ON DELETE CASCADE,
          version_number INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','ACTIVE','SUPERSEDED')),
          source_storage_key TEXT, source_filename TEXT, source_checksum TEXT,
          raw_text TEXT, structured_data JSONB, extracted_metadata JSONB,
          metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(), published_at TIMESTAMPTZ,
          UNIQUE(job_description_id, version_number)
        );
        CREATE UNIQUE INDEX ux_job_description_versions_active
          ON job_description_versions(job_description_id) WHERE status = 'ACTIVE';
        CREATE INDEX ix_job_description_versions_job_created
          ON job_description_versions(job_description_id, version_number DESC);
        ALTER TABLE job_descriptions ADD COLUMN active_version_id UUID;
        """
    )
    op.execute(
        """
        INSERT INTO job_description_versions
          (job_description_id, version_number, status, source_storage_key, source_filename,
           source_checksum, raw_text, structured_data, extracted_metadata, metadata, published_at)
        SELECT id, 1, CASE WHEN listing_status = 'ACTIVE' THEN 'ACTIVE' ELSE 'DRAFT' END,
          storage_key, filename, checksum, raw_text, structured_data, extracted_metadata,
          jsonb_build_object('title', title, 'description', description, 'keywords', keywords,
            'companyName', company_name, 'location', location),
          CASE WHEN listing_status = 'ACTIVE' THEN now() ELSE NULL END
        FROM job_descriptions WHERE item_type = 'JOB_DESCRIPTION';
        UPDATE job_descriptions jd SET active_version_id = v.id
          FROM job_description_versions v
          WHERE v.job_description_id = jd.id AND v.status = 'ACTIVE';
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE job_descriptions DROP COLUMN active_version_id")
    op.execute("DROP TABLE job_description_versions")
