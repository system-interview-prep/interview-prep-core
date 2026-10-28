"""Weekly practice goal per candidate, for the weekly practice streak.

Revision ID: 20261019_0028
Revises: 20261016_0027
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261019_0028"
down_revision: str | None = "20261016_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE users
          ADD COLUMN practice_weekly_goal SMALLINT NOT NULL DEFAULT 2
            CHECK (practice_weekly_goal BETWEEN 1 AND 7);
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN practice_weekly_goal;")
