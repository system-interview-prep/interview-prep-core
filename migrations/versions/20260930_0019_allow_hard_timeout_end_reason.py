"""Allow HARD_TIMEOUT end reason in interview_sessions.

Revision ID: 20260930_0019
Revises: 20260927_0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0019"
down_revision: str | None = "20260927_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_interview_sessions_end_reason", "interview_sessions", type_="check")
    op.create_check_constraint(
        "ck_interview_sessions_end_reason",
        "interview_sessions",
        "end_reason IS NULL OR end_reason IN ('COMPLETED', 'USER_ENDED', 'TECHNICAL_FAILURE', 'HARD_TIMEOUT')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_interview_sessions_end_reason", "interview_sessions", type_="check")
    op.create_check_constraint(
        "ck_interview_sessions_end_reason",
        "interview_sessions",
        "end_reason IS NULL OR end_reason IN ('COMPLETED', 'USER_ENDED', 'TECHNICAL_FAILURE')",
    )
