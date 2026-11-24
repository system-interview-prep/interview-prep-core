"""CV pipeline events for the admin CV-parsing view; ``ai_call_logs.cv_id``.

One row per event of a CV upload or parse attempt (upload, queue, extract,
source, parse, quality, save, done). ``attempt_id`` groups the events of one
upload or one ``CvParsingPipeline.run`` call. Like the other pipeline event
tables there are no foreign keys: rows are written by a background flusher and
old rows are removed by the retention window (PIPELINE_EVENTS_RETENTION_DAYS).

``ai_call_logs.cv_id`` links each model call made while parsing a CV to it.

Revision ID: 20261020_0033
Revises: 20261019_0032
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261020_0033"
down_revision: str | None = "20261019_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE cv_pipeline_events (
          id BIGSERIAL PRIMARY KEY,
          cv_id VARCHAR(36) NOT NULL,
          attempt_id VARCHAR(36) NOT NULL,
          user_id VARCHAR(36),
          stage VARCHAR(24) NOT NULL,
          event VARCHAR(64) NOT NULL,
          status VARCHAR(16) NOT NULL
            CHECK (status IN ('started', 'completed', 'error', 'warning', 'info', 'skipped')),
          duration_ms INTEGER,
          payload JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_cv_events_cv ON cv_pipeline_events(cv_id, created_at);
        CREATE INDEX ix_cv_events_created ON cv_pipeline_events(created_at);
        CREATE INDEX ix_cv_events_user ON cv_pipeline_events(user_id);

        ALTER TABLE ai_call_logs ADD COLUMN IF NOT EXISTS cv_id VARCHAR(36);
        CREATE INDEX IF NOT EXISTS ix_ai_call_logs_cv ON ai_call_logs(cv_id, created_at);
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS ix_ai_call_logs_cv; "
        "ALTER TABLE ai_call_logs DROP COLUMN IF EXISTS cv_id; "
        "DROP TABLE IF EXISTS cv_pipeline_events;"
    )
