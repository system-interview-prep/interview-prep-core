"""Persist the interview chat clock separately from session creation.

Revision ID: 20260925_0016
Revises: 20260925_0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260925_0016"
down_revision: str | None = "20260925_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("interview_sessions", sa.Column("chat_started_at", sa.DateTime(timezone=True)))
    op.add_column("interview_sessions", sa.Column("chat_deadline_at", sa.DateTime(timezone=True)))
    op.drop_constraint("ck_interview_sessions_end_reason", "interview_sessions", type_="check")
    op.create_check_constraint(
        "ck_interview_sessions_end_reason",
        "interview_sessions",
        "end_reason IS NULL OR end_reason IN ('COMPLETED', 'USER_ENDED', 'TECHNICAL_FAILURE', 'TIME_EXPIRED')",
    )
    # An existing active chat starts its clock at its first persisted chat message.
    op.execute("""
        UPDATE interview_sessions s
        SET chat_started_at = m.first_message_at,
            chat_deadline_at = m.first_message_at + s.duration_minutes * interval '1 minute'
        FROM (
            SELECT session_id, MIN(created_at) AS first_message_at
            FROM interview_chat_messages GROUP BY session_id
        ) m
        WHERE s.id = m.session_id AND s.experience_type = 'interview_chat'
    """)


def downgrade() -> None:
    op.drop_constraint("ck_interview_sessions_end_reason", "interview_sessions", type_="check")
    op.create_check_constraint(
        "ck_interview_sessions_end_reason",
        "interview_sessions",
        "end_reason IS NULL OR end_reason IN ('COMPLETED', 'USER_ENDED', 'TECHNICAL_FAILURE')",
    )
    op.drop_column("interview_sessions", "chat_deadline_at")
    op.drop_column("interview_sessions", "chat_started_at")
