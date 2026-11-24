"""Keep the latest CV–JD fit summary per candidate and job.

Revision ID: 20261103_0038
Revises: 20261026_0037
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261103_0038"
down_revision: str | None = "20261026_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_job_fit_history (
          user_id VARCHAR(36) NOT NULL,
          job_id VARCHAR(36) NOT NULL,
          cv_id VARCHAR(36) NOT NULL,
          met INTEGER NOT NULL CHECK (met >= 0),
          total INTEGER NOT NULL CHECK (total > 0),
          analyzed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (user_id, job_id),
          CHECK (met <= total)
        );
        CREATE INDEX IF NOT EXISTS ix_candidate_job_fit_history_user_date
          ON candidate_job_fit_history(user_id, analyzed_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS candidate_job_fit_history")
