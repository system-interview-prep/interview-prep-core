"""Add published listing fields, lifecycle statuses, constraints, and deterministic status backfill.

Revision ID: 20260921_0009
Revises: 20260909_0008
Create Date: 2026-09-21 13:30:00.000000

NOTE ON DOWNGRADE:
Downgrading this migration is DESTRUCTIVE to all newly introduced listing fields.
Dropping the new columns permanently discards any populated published data,
source identity metadata, and separated lifecycle statuses.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260921_0009"
down_revision = "20260909_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add top-level published listing columns
    op.add_column("job_descriptions", sa.Column("external_job_id", sa.String(255), nullable=True))
    op.add_column("job_descriptions", sa.Column("company_name", sa.String(255), nullable=True))
    op.add_column("job_descriptions", sa.Column("company_logo_url", sa.Text(), nullable=True))
    op.add_column("job_descriptions", sa.Column("location", sa.String(255), nullable=True))
    op.add_column("job_descriptions", sa.Column("work_mode", sa.String(32), nullable=True))
    op.add_column("job_descriptions", sa.Column("employment_type", sa.String(32), nullable=True))
    op.add_column("job_descriptions", sa.Column("seniority", sa.String(32), nullable=True))
    op.add_column("job_descriptions", sa.Column("experience_min_years", sa.SmallInteger(), nullable=True))
    op.add_column("job_descriptions", sa.Column("experience_max_years", sa.SmallInteger(), nullable=True))

    # 2. Canonical salary (No defaults, tri-state negotiable)
    op.add_column("job_descriptions", sa.Column("salary_min", sa.BigInteger(), nullable=True))
    op.add_column("job_descriptions", sa.Column("salary_max", sa.BigInteger(), nullable=True))
    op.add_column("job_descriptions", sa.Column("salary_currency", sa.String(10), nullable=True))
    op.add_column("job_descriptions", sa.Column("salary_period", sa.String(20), nullable=True))
    op.add_column("job_descriptions", sa.Column("salary_negotiable", sa.Boolean(), nullable=True))

    # 3. Source identity and ingestion tracking (No default now for internal upload)
    op.add_column("job_descriptions", sa.Column("source_type", sa.String(32), server_default="internal_upload", nullable=False))
    op.add_column("job_descriptions", sa.Column("source_key", sa.String(100), server_default="default", nullable=False))
    op.add_column("job_descriptions", sa.Column("source_name", sa.String(100), nullable=True))
    op.add_column("job_descriptions", sa.Column("source_url", sa.Text(), nullable=True))
    op.add_column("job_descriptions", sa.Column("apply_url", sa.Text(), nullable=True))
    op.add_column("job_descriptions", sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("job_descriptions", sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("job_descriptions", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("job_descriptions", sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True))

    # 4. Separated lifecycle statuses
    op.add_column("job_descriptions", sa.Column("processing_status", sa.String(32), server_default="PENDING", nullable=False))
    op.add_column("job_descriptions", sa.Column("listing_status", sa.String(32), server_default="DRAFT", nullable=False))

    # 5. Deterministic SQL Lifecycle Backfill for existing rows
    # (Pure SQL status mapping, no parser/extraction/business inference logic)
    op.execute(
        """
        UPDATE job_descriptions
        SET processing_status = 'DONE',
            listing_status = CASE
                WHEN status = 'ACTIVE' THEN 'ACTIVE'
                WHEN status = 'ARCHIVED' THEN 'ARCHIVED'
                ELSE 'DRAFT'
            END
        WHERE item_type = 'JOB_DESCRIPTION';
        """
    )
    op.execute(
        """
        UPDATE job_descriptions
        SET listing_status = 'DRAFT',
            processing_status = CASE
                WHEN status = 'DONE' THEN 'DONE'
                WHEN status = 'FAILED' THEN 'FAILED'
                WHEN status = 'PROCESSING' THEN 'PROCESSING'
                ELSE 'PENDING'
            END
        WHERE item_type = 'JD_UPLOAD';
        """
    )

    # 6. Check Constraints
    op.create_check_constraint(
        "ck_job_descriptions_work_mode",
        "job_descriptions",
        "work_mode IS NULL OR work_mode IN ('remote', 'hybrid', 'on_site')",
    )
    op.create_check_constraint(
        "ck_job_descriptions_employment_type",
        "job_descriptions",
        "employment_type IS NULL OR employment_type IN ('full_time', 'part_time', 'internship', 'contract', 'temporary')",
    )
    op.create_check_constraint(
        "ck_job_descriptions_seniority",
        "job_descriptions",
        "seniority IS NULL OR seniority IN ('intern', 'fresher', 'junior', 'mid', 'senior', 'lead', 'manager')",
    )
    op.create_check_constraint(
        "ck_job_descriptions_salary_period",
        "job_descriptions",
        "salary_period IS NULL OR salary_period IN ('hour', 'month', 'year')",
    )
    op.create_check_constraint(
        "ck_job_descriptions_source_type",
        "job_descriptions",
        "source_type IN ('internal_upload', 'manual', 'greenhouse', 'lever', 'company_career', 'other')",
    )
    op.create_check_constraint(
        "ck_job_descriptions_processing_status",
        "job_descriptions",
        "processing_status IN ('PENDING', 'PROCESSING', 'DONE', 'FAILED')",
    )
    op.create_check_constraint(
        "ck_job_descriptions_listing_status",
        "job_descriptions",
        "listing_status IN ('DRAFT', 'ACTIVE', 'ARCHIVED', 'CLOSED', 'EXPIRED')",
    )
    op.create_check_constraint(
        "ck_job_descriptions_salary_range",
        "job_descriptions",
        "salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max",
    )
    op.create_check_constraint(
        "ck_job_descriptions_experience_range",
        "job_descriptions",
        "experience_min_years IS NULL OR experience_max_years IS NULL OR experience_min_years <= experience_max_years",
    )
    op.create_check_constraint(
        "ck_job_descriptions_source_key_not_default",
        "job_descriptions",
        "external_job_id IS NULL OR source_type IN ('internal_upload', 'manual', 'other') OR source_key != 'default'",
    )

    # 7. Indexes
    op.create_index("ix_job_descriptions_company_name", "job_descriptions", ["company_name"])
    op.create_index("ix_job_descriptions_listing_status_created", "job_descriptions", ["listing_status", "created_at"])
    op.create_index("ix_job_descriptions_processing_status", "job_descriptions", ["processing_status"])

    # 8. Multi-tenant Source Identity Uniqueness Constraint
    op.create_index(
        "uq_job_descriptions_source_identity",
        "job_descriptions",
        ["source_type", "source_key", "external_job_id"],
        unique=True,
        postgresql_where=sa.text("external_job_id IS NOT NULL"),
    )


def downgrade() -> None:
    # 1. Drop Indexes
    op.drop_index("uq_job_descriptions_source_identity", table_name="job_descriptions")
    op.drop_index("ix_job_descriptions_processing_status", table_name="job_descriptions")
    op.drop_index("ix_job_descriptions_listing_status_created", table_name="job_descriptions")
    op.drop_index("ix_job_descriptions_company_name", table_name="job_descriptions")

    # 2. Drop Check Constraints
    op.drop_constraint("ck_job_descriptions_source_key_not_default", "job_descriptions", type_="check")
    op.drop_constraint("ck_job_descriptions_experience_range", "job_descriptions", type_="check")
    op.drop_constraint("ck_job_descriptions_salary_range", "job_descriptions", type_="check")
    op.drop_constraint("ck_job_descriptions_listing_status", "job_descriptions", type_="check")
    op.drop_constraint("ck_job_descriptions_processing_status", "job_descriptions", type_="check")
    op.drop_constraint("ck_job_descriptions_source_type", "job_descriptions", type_="check")
    op.drop_constraint("ck_job_descriptions_salary_period", "job_descriptions", type_="check")
    op.drop_constraint("ck_job_descriptions_seniority", "job_descriptions", type_="check")
    op.drop_constraint("ck_job_descriptions_employment_type", "job_descriptions", type_="check")
    op.drop_constraint("ck_job_descriptions_work_mode", "job_descriptions", type_="check")

    # 3. Drop Columns (Destructive to newly added data)
    op.drop_column("job_descriptions", "listing_status")
    op.drop_column("job_descriptions", "processing_status")
    op.drop_column("job_descriptions", "fetched_at")
    op.drop_column("job_descriptions", "last_seen_at")
    op.drop_column("job_descriptions", "first_seen_at")
    op.drop_column("job_descriptions", "posted_at")
    op.drop_column("job_descriptions", "apply_url")
    op.drop_column("job_descriptions", "source_url")
    op.drop_column("job_descriptions", "source_name")
    op.drop_column("job_descriptions", "source_key")
    op.drop_column("job_descriptions", "source_type")
    op.drop_column("job_descriptions", "salary_negotiable")
    op.drop_column("job_descriptions", "salary_period")
    op.drop_column("job_descriptions", "salary_currency")
    op.drop_column("job_descriptions", "salary_max")
    op.drop_column("job_descriptions", "salary_min")
    op.drop_column("job_descriptions", "experience_max_years")
    op.drop_column("job_descriptions", "experience_min_years")
    op.drop_column("job_descriptions", "seniority")
    op.drop_column("job_descriptions", "employment_type")
    op.drop_column("job_descriptions", "work_mode")
    op.drop_column("job_descriptions", "location")
    op.drop_column("job_descriptions", "company_logo_url")
    op.drop_column("job_descriptions", "company_name")
    op.drop_column("job_descriptions", "external_job_id")
