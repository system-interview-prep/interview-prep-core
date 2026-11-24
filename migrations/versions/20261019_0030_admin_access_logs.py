"""Admin access log for candidate data, and an event-name index for live views.

``admin_access_logs`` records every time an admin reads a candidate's own
words (the answers of one question in the pipeline view): who, what, when.
It is shown on /admin/audit-logs. The event retention job does not touch it:
an audit trail must outlive the data it audits.

``ix_pipeline_events_event`` serves the live board and the alerts, which look
up the latest ``voice_state`` per session and the last hour of ``turn_spoken``.

Revision ID: 20261019_0030
Revises: 20261019_0029
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261019_0030"
down_revision: str | None = "20261019_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE admin_access_logs (
          id BIGSERIAL PRIMARY KEY,
          admin_user_id VARCHAR(36) NOT NULL,
          admin_email VARCHAR(320),
          action VARCHAR(64) NOT NULL,
          session_id VARCHAR(36),
          target_id VARCHAR(64),
          detail JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_admin_access_logs_created ON admin_access_logs(created_at);
        CREATE INDEX ix_admin_access_logs_session ON admin_access_logs(session_id, created_at);
        CREATE INDEX ix_pipeline_events_event ON interview_pipeline_events(event, created_at);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_pipeline_events_event; DROP TABLE IF EXISTS admin_access_logs;")
