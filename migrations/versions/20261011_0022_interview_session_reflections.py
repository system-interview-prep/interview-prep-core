"""Store the candidate's own reflection on a finished interview.

Revision ID: 20261011_0022
Revises: 20261010_0021

The debrief asks the candidate to rate each answer before the system score is
revealed, and to write one takeaway. Keeping both server-side lets later
features compare self-assessment with scores across sessions.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261011_0022"
down_revision: str | None = "20261010_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "interview_session_reflections",
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("interview_sessions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("user_id", sa.String(36), nullable=False),
        # {turnId: 1..4}, same 4-level scale the debrief shows.
        sa.Column("self_ratings", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("takeaway", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_interview_session_reflections_user", "interview_session_reflections", ["user_id", "updated_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_interview_session_reflections_user", table_name="interview_session_reflections")
    op.drop_table("interview_session_reflections")
