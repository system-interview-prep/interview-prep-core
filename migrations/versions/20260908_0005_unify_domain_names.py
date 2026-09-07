"""Use job description and storage terminology consistently.

Revision ID: 20260908_0005
Revises: 20260908_0004
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260908_0005"
down_revision: str | None = "20260908_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("user_cvs", "s3_key", new_column_name="storage_key")

    op.rename_table("job_profiles", "job_descriptions")
    op.alter_column("job_descriptions", "s3_key", new_column_name="storage_key")
    op.alter_column("job_descriptions", "raw_jd_text", new_column_name="raw_text")
    op.alter_column("job_descriptions", "ai_profile_ui_json", new_column_name="structured_data")
    op.alter_column("job_descriptions", "ai_extras_json", new_column_name="extracted_metadata")
    op.alter_column("job_descriptions", "ai_profile_version", new_column_name="extraction_version")
    op.alter_column("job_descriptions", "ai_generated_at", new_column_name="extracted_at")
    op.execute("UPDATE job_descriptions SET item_type = 'JD_UPLOAD' WHERE item_type = 'JP_UPLOAD'")
    op.execute(
        "UPDATE job_descriptions SET item_type = 'JOB_DESCRIPTION' WHERE item_type = 'JOBPROFILE'"
    )
    op.execute("UPDATE interview_sessions SET status = upper(status)")
    op.execute("UPDATE video_calls SET status = upper(status)")

    op.rename_table("job_profiles_vector", "job_descriptions_vector")
    op.alter_column(
        "job_descriptions_vector", "job_id", new_column_name="job_description_id"
    )
    op.alter_column(
        "job_descriptions_vector", "job_vector", new_column_name="job_description_vector"
    )

    op.execute("ALTER INDEX ix_job_profiles_category_created RENAME TO ix_job_descriptions_category_created")
    op.execute("ALTER INDEX ix_job_profiles_status_created RENAME TO ix_job_descriptions_status_created")
    op.execute("ALTER INDEX ix_job_profiles_search RENAME TO ix_job_descriptions_search")
    op.execute("ALTER INDEX job_profiles_bm25_idx RENAME TO job_descriptions_bm25_idx")


def downgrade() -> None:
    op.execute("ALTER INDEX job_descriptions_bm25_idx RENAME TO job_profiles_bm25_idx")
    op.execute("ALTER INDEX ix_job_descriptions_search RENAME TO ix_job_profiles_search")
    op.execute("ALTER INDEX ix_job_descriptions_status_created RENAME TO ix_job_profiles_status_created")
    op.execute("ALTER INDEX ix_job_descriptions_category_created RENAME TO ix_job_profiles_category_created")

    op.alter_column(
        "job_descriptions_vector", "job_description_vector", new_column_name="job_vector"
    )
    op.alter_column(
        "job_descriptions_vector", "job_description_id", new_column_name="job_id"
    )
    op.rename_table("job_descriptions_vector", "job_profiles_vector")

    op.execute("UPDATE interview_sessions SET status = initcap(status)")
    op.execute("UPDATE video_calls SET status = lower(status)")

    op.execute(
        "UPDATE job_descriptions SET item_type = 'JOBPROFILE' WHERE item_type = 'JOB_DESCRIPTION'"
    )
    op.execute("UPDATE job_descriptions SET item_type = 'JP_UPLOAD' WHERE item_type = 'JD_UPLOAD'")
    op.alter_column("job_descriptions", "extracted_at", new_column_name="ai_generated_at")
    op.alter_column("job_descriptions", "extraction_version", new_column_name="ai_profile_version")
    op.alter_column("job_descriptions", "extracted_metadata", new_column_name="ai_extras_json")
    op.alter_column("job_descriptions", "structured_data", new_column_name="ai_profile_ui_json")
    op.alter_column("job_descriptions", "raw_text", new_column_name="raw_jd_text")
    op.alter_column("job_descriptions", "storage_key", new_column_name="s3_key")
    op.rename_table("job_descriptions", "job_profiles")
    op.alter_column("user_cvs", "storage_key", new_column_name="s3_key")
