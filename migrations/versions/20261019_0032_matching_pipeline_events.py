"""Matching pipeline events for the admin matching-run view; ``ai_call_logs.match_run_id``.

One row per run-level event of a CV-JD matching run (request, inputs, cache,
targeted reparse, requirements, semantic, scoring, cache write, done, and the
clarification gate / questions / rescore). Like interview_pipeline_events
there are no foreign keys: rows are written by a background flusher and old
rows are removed by the retention window (PIPELINE_EVENTS_RETENTION_DAYS).

``ai_call_logs.match_run_id`` links each model call made during a run to it.

Revision ID: 20261019_0032
Revises: 20261019_0031
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261019_0032"
down_revision: str | None = "20261019_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE matching_pipeline_events (
          id BIGSERIAL PRIMARY KEY,
          run_id VARCHAR(36) NOT NULL,
          flow VARCHAR(24) NOT NULL,
          cache_key CHAR(64),
          user_id VARCHAR(36),
          resume_id VARCHAR(64),
          job_id VARCHAR(64),
          stage VARCHAR(24) NOT NULL,
          event VARCHAR(64) NOT NULL,
          status VARCHAR(16) NOT NULL
            CHECK (status IN ('started', 'completed', 'error', 'warning', 'info', 'skipped')),
          duration_ms INTEGER,
          payload JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_matching_events_run ON matching_pipeline_events(run_id, created_at);
        CREATE INDEX ix_matching_events_created ON matching_pipeline_events(created_at);
        CREATE INDEX ix_matching_events_resume_job
          ON matching_pipeline_events(resume_id, job_id, created_at);
        CREATE INDEX ix_matching_events_user ON matching_pipeline_events(user_id, created_at);

        ALTER TABLE ai_call_logs ADD COLUMN IF NOT EXISTS match_run_id VARCHAR(36);
        CREATE INDEX IF NOT EXISTS ix_ai_call_logs_match_run ON ai_call_logs(match_run_id, created_at);
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS ix_ai_call_logs_match_run; "
        "ALTER TABLE ai_call_logs DROP COLUMN IF EXISTS match_run_id; "
        "DROP TABLE IF EXISTS matching_pipeline_events;"
    )
