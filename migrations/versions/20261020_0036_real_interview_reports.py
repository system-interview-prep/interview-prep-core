"""Real interviews the candidate reports after practising.

The progress page says whether a candidate is ready for a position. Nothing
checked that claim against real interviews (research 20/10, "Ứng viên có quan
tâm không"). ``real_interview_reports`` keeps what the candidate tells us after
a real interview: the round, the result, which rounds were asked and what was
hard, with a snapshot of the readiness the page showed at that time
(``readiness``), so readiness can later be compared with real results.

Revision ID: 20261020_0036
Revises: 20261020_0035
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261020_0036"
down_revision: str | None = "20261020_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS real_interview_reports (
          id VARCHAR(36) PRIMARY KEY,
          user_id VARCHAR(36) NOT NULL,
          job_id VARCHAR(36),
          company VARCHAR(160),
          interview_date DATE NOT NULL,
          stage VARCHAR(24) NOT NULL,
          result VARCHAR(16) NOT NULL,
          asked_areas JSONB NOT NULL DEFAULT '[]'::jsonb,
          hard_parts TEXT,
          readiness JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_real_interview_reports_user
          ON real_interview_reports(user_id, interview_date DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS real_interview_reports;")
