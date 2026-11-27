"""Scope a clarification session to one matching run.

The first version of this table was keyed on (user, cv, job), which has no
notion of a run: starting a fresh assessment of the same CV against the same
posting restored the previous run's questions and the answers given to them,
as though nothing new had been asked.

A run already has an identity - the row ``candidate_job_fit_attempts`` writes
for each assessment - so that is the key, and the two stay together: deleting
an attempt takes its clarifications with it.  Every run keeps its own
questions and answers, so the assessment history shows what was actually asked
and answered at the time rather than the latest state overwriting the record.

The handful of rows written under the old key belong to no run and cannot be
assigned one, so they are dropped rather than guessed at.

Revision ID: 20261127_0046
Revises: 20261127_0045
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261127_0046"
down_revision: str | None = "20261127_0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM clarification_sessions;

        ALTER TABLE clarification_sessions
          ADD COLUMN IF NOT EXISTS fit_attempt_id UUID NOT NULL
            REFERENCES candidate_job_fit_attempts(id) ON DELETE CASCADE;

        DROP INDEX IF EXISTS ux_clarification_sessions_scope;
        DROP INDEX IF EXISTS ix_clarification_sessions_lookup;

        -- One live question per requirement within one run.
        CREATE UNIQUE INDEX IF NOT EXISTS ux_clarification_sessions_attempt_requirement
          ON clarification_sessions(fit_attempt_id, requirement_id);
        -- The read a reload performs: this run's questions, in the order asked.
        CREATE INDEX IF NOT EXISTS ix_clarification_sessions_attempt
          ON clarification_sessions(fit_attempt_id, created_at);
        -- Every run recorded for one CV-to-JD pairing, newest first.
        CREATE INDEX IF NOT EXISTS ix_clarification_sessions_pairing
          ON clarification_sessions(user_id, cv_id, job_id, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS ux_clarification_sessions_attempt_requirement;
        DROP INDEX IF EXISTS ix_clarification_sessions_attempt;
        DROP INDEX IF EXISTS ix_clarification_sessions_pairing;

        ALTER TABLE clarification_sessions DROP COLUMN IF EXISTS fit_attempt_id;

        CREATE UNIQUE INDEX IF NOT EXISTS ux_clarification_sessions_scope
          ON clarification_sessions(user_id, cv_id, job_id, requirement_id);
        CREATE INDEX IF NOT EXISTS ix_clarification_sessions_lookup
          ON clarification_sessions(user_id, cv_id, job_id, created_at DESC);
        """
    )
