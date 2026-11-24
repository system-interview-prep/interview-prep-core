"""Add durable leases for interview evaluation jobs.

Revision ID: 20261013_0024
Revises: 20261012_0023
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261013_0024"
down_revision: str | None = "20261012_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE interview_evaluation_jobs (
          session_id VARCHAR(36) PRIMARY KEY
            REFERENCES interview_sessions(id) ON DELETE CASCADE,
          status VARCHAR(16) NOT NULL
            CHECK (status IN ('PROCESSING', 'DONE', 'FAILED')),
          lease_until TIMESTAMPTZ NOT NULL,
          attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count > 0),
          last_error TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_interview_evaluation_jobs_status_lease
          ON interview_evaluation_jobs(status, lease_until);
        CREATE UNIQUE INDEX ux_interview_turns_one_asked
          ON interview_turns(session_id) WHERE status = 'ASKED';
        CREATE UNIQUE INDEX ux_interview_turn_evaluations_eval_turn
          ON interview_turn_evaluations(evaluation_id, turn_id);
        ALTER TABLE interview_turn_evaluations
          ADD CONSTRAINT ck_interview_turn_evaluations_score CHECK (score >= 0 AND score <= 10);
        ALTER TABLE interview_evaluations
          ADD CONSTRAINT ck_interview_evaluations_score CHECK (overall_score >= 0 AND overall_score <= 10);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE interview_evaluations DROP CONSTRAINT ck_interview_evaluations_score;
        ALTER TABLE interview_turn_evaluations DROP CONSTRAINT ck_interview_turn_evaluations_score;
        DROP INDEX ux_interview_turn_evaluations_eval_turn;
        DROP INDEX ux_interview_turns_one_asked;
        DROP TABLE interview_evaluation_jobs;
        """
    )
