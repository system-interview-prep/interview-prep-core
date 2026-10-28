"""Record whether an evaluated turn was answered, skipped by the candidate, or could not be graded.

Revision ID: 20261016_0025
Revises: 20261013_0024
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261016_0025"
down_revision: str | None = "20261013_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE interview_turn_evaluations
          ADD COLUMN outcome VARCHAR(16) NOT NULL DEFAULT 'ANSWERED'
            CHECK (outcome IN ('ANSWERED', 'SKIPPED', 'NOT_GRADABLE'));
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE interview_turn_evaluations DROP COLUMN outcome;")
