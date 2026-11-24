"""Cache matching results by request hash so the same CV and JD give the same match.

Revision ID: 20261016_0026
Revises: 20261016_0025
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261016_0026"
down_revision: str | None = "20261016_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE matching_result_cache (
          cache_key CHAR(64) PRIMARY KEY,
          resume_id VARCHAR(64) NOT NULL,
          job_id VARCHAR(64) NOT NULL,
          -- JSON, not JSONB: keeps key order so a cached match reads back byte-identical.
          result JSON NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_matching_result_cache_resume_job
          ON matching_result_cache(resume_id, job_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE matching_result_cache;")
