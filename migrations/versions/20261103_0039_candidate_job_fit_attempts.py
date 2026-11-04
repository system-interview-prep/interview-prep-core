"""Keep each candidate CV-to-JD assessment for the history modal.

Revision ID: 20261103_0039
Revises: 20261103_0038
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261103_0039"
down_revision: str | None = "20261103_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_job_fit_attempts (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id VARCHAR(36) NOT NULL,
          job_id VARCHAR(36) NOT NULL,
          cv_id VARCHAR(36) NOT NULL,
          met INTEGER NOT NULL CHECK (met >= 0),
          total INTEGER NOT NULL CHECK (total > 0),
          analyzed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CHECK (met <= total)
        );
        CREATE INDEX IF NOT EXISTS ix_candidate_job_fit_attempts_user_job_date
          ON candidate_job_fit_attempts(user_id, job_id, analyzed_at DESC);

        -- The previous table kept only the latest assessment per job. Preserve
        -- that row as the first visible history entry during migration.
        INSERT INTO candidate_job_fit_attempts
          (user_id, job_id, cv_id, met, total, analyzed_at)
        SELECT user_id, job_id, cv_id, met, total, analyzed_at
        FROM candidate_job_fit_history;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS candidate_job_fit_attempts")
