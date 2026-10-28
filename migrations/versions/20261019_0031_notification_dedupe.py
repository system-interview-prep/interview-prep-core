"""One notification per problem per admin: ``notifications.dedupe_key``.

Admin notifications are created from the AI alerts (src/modules/interviews/
application/admin_notifier.py), which are re-evaluated every few seconds. The
unique (user_id, dedupe_key) index makes each problem notify each admin once,
even when two API processes evaluate the alerts at the same moment. Existing
user notifications keep a NULL key and are unaffected.

Revision ID: 20261019_0031
Revises: 20261019_0030
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261019_0031"
down_revision: str | None = "20261019_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE notifications ADD COLUMN IF NOT EXISTS dedupe_key VARCHAR(160);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_notifications_user_dedupe
          ON notifications(user_id, dedupe_key) WHERE dedupe_key IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS ux_notifications_user_dedupe; "
        "ALTER TABLE notifications DROP COLUMN IF EXISTS dedupe_key;"
    )
