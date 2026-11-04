"""Save the CV-JD result snapshot for opening a past assessment.

Revision ID: 20261103_0040
Revises: 20261103_0039
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261103_0040"
down_revision: str | None = "20261103_0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE candidate_job_fit_attempts "
        "ADD COLUMN IF NOT EXISTS result_snapshot JSONB"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE candidate_job_fit_attempts "
        "DROP COLUMN IF EXISTS result_snapshot"
    )
