"""Allow 2-minute interview sessions for live demos.

Revision ID: 20261010_0021
Revises: 20261002_0020

The runtime's pacing thresholds now scale with the session length, so a
2-3 minute session still reaches a technical question. Downgrading moves any
session shorter than 5 minutes up to 5 to satisfy the previous constraint.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261010_0021"
down_revision: str | None = "20261002_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_interview_sessions_duration", "interview_sessions", type_="check")
    op.create_check_constraint(
        "ck_interview_sessions_duration",
        "interview_sessions",
        "duration_minutes BETWEEN 2 AND 120",
    )


def downgrade() -> None:
    op.execute("UPDATE interview_sessions SET duration_minutes = 5 WHERE duration_minutes < 5")
    op.drop_constraint("ck_interview_sessions_duration", "interview_sessions", type_="check")
    op.create_check_constraint(
        "ck_interview_sessions_duration",
        "interview_sessions",
        "duration_minutes BETWEEN 5 AND 120",
    )
