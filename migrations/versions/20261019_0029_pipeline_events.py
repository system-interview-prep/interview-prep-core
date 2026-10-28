"""Interview pipeline events and AI call logs for the admin pipeline view.

No foreign key to interview_sessions: events are written by a background
flusher, possibly before the session row commits, and a rejected row must not
drop the whole batch. Old rows are removed by the retention window
(PIPELINE_EVENTS_RETENTION_DAYS) instead of a cascade.

Revision ID: 20261019_0029
Revises: 20261019_0028
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261019_0029"
down_revision: str | None = "20261019_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE interview_pipeline_events (
          id BIGSERIAL PRIMARY KEY,
          session_id VARCHAR(36) NOT NULL,
          stage VARCHAR(24) NOT NULL,
          event VARCHAR(64) NOT NULL,
          status VARCHAR(12) NOT NULL
            CHECK (status IN ('started', 'completed', 'error', 'warning', 'info')),
          turn_index INTEGER,
          duration_ms INTEGER,
          payload JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_pipeline_events_session ON interview_pipeline_events(session_id, created_at);
        CREATE INDEX ix_pipeline_events_created ON interview_pipeline_events(created_at);
        CREATE INDEX ix_pipeline_events_stage_status ON interview_pipeline_events(stage, status, created_at);

        CREATE TABLE ai_call_logs (
          id BIGSERIAL PRIMARY KEY,
          session_id VARCHAR(36),
          purpose VARCHAR(160) NOT NULL,
          provider VARCHAR(32) NOT NULL,
          model VARCHAR(120),
          status VARCHAR(12) NOT NULL CHECK (status IN ('ok', 'error')),
          duration_ms INTEGER NOT NULL,
          input_tokens INTEGER,
          output_tokens INTEGER,
          input_chars INTEGER,
          output_chars INTEGER,
          error_type VARCHAR(120),
          error_message TEXT,
          input_preview TEXT,
          output_preview TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_ai_call_logs_session ON ai_call_logs(session_id, created_at);
        CREATE INDEX ix_ai_call_logs_created ON ai_call_logs(created_at);
        CREATE INDEX ix_ai_call_logs_purpose ON ai_call_logs(purpose, created_at);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai_call_logs; DROP TABLE IF EXISTS interview_pipeline_events;")
