"""Make taxonomy the sole classification source for job descriptions."""

from alembic import op

revision = "20260909_0008"
down_revision = "20260909_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE job_descriptions ADD COLUMN primary_taxonomy_version TEXT;
    ALTER TABLE job_descriptions ADD COLUMN primary_taxonomy_concept_id TEXT;
    CREATE INDEX ix_job_descriptions_taxonomy_created ON job_descriptions (primary_taxonomy_version, primary_taxonomy_concept_id, created_at);
    """)


def downgrade() -> None:
    op.execute("DROP INDEX ix_job_descriptions_taxonomy_created;")
    op.execute("ALTER TABLE job_descriptions DROP COLUMN primary_taxonomy_concept_id;")
    op.execute("ALTER TABLE job_descriptions DROP COLUMN primary_taxonomy_version;")
