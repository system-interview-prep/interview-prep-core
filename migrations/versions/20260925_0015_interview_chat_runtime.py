"""Interview chat runtime, session classification, and dedicated chat messages.

Revision ID: 20260925_0015
Revises: 20260925_0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_0015"
down_revision: str | None = "20260925_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add experience_type and end_reason to interview_sessions
    op.add_column(
        "interview_sessions",
        sa.Column("experience_type", sa.String(32), nullable=True),
    )
    op.add_column(
        "interview_sessions",
        sa.Column("end_reason", sa.String(32), nullable=True),
    )

    # 2. Backfill historical sessions according to actual origin
    # Legacy unstructured sessions created via /ai/session
    op.execute(
        "UPDATE interview_sessions "
        "SET experience_type = 'legacy_unstructured' "
        "WHERE resume_id IS NULL OR job_id IS NULL"
    )
    # Grounded structured practice text sessions
    op.execute(
        "UPDATE interview_sessions "
        "SET experience_type = 'question_practice' "
        "WHERE resume_id IS NOT NULL AND job_id IS NOT NULL AND mode = 'text' AND experience_type IS NULL"
    )
    # Voice sessions
    op.execute(
        "UPDATE interview_sessions "
        "SET experience_type = 'voice_interview' "
        "WHERE mode = 'voice' AND experience_type IS NULL"
    )
    # Video sessions
    op.execute(
        "UPDATE interview_sessions "
        "SET experience_type = 'video_interview' "
        "WHERE mode = 'video' AND experience_type IS NULL"
    )
    # Fallback default if any session remains unclassified
    op.execute(
        "UPDATE interview_sessions "
        "SET experience_type = 'legacy_unstructured' "
        "WHERE experience_type IS NULL"
    )

    # 3. Alter experience_type to NOT NULL and default to 'interview_chat'
    op.alter_column("interview_sessions", "experience_type", nullable=False, server_default="interview_chat")

    op.create_check_constraint(
        "ck_interview_sessions_experience_type",
        "interview_sessions",
        "experience_type IN ('legacy_unstructured', 'question_practice', 'voice_interview', 'video_interview', 'interview_chat')",
    )
    op.create_check_constraint(
        "ck_interview_sessions_end_reason",
        "interview_sessions",
        "end_reason IS NULL OR end_reason IN ('COMPLETED', 'USER_ENDED', 'TECHNICAL_FAILURE')",
    )
    op.create_index(
        "ix_interview_sessions_experience_type",
        "interview_sessions",
        ["experience_type"],
    )

    # 4. Create dedicated interview_chat_messages table
    op.create_table(
        "interview_chat_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("interview_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "turn_id",
            sa.String(36),
            sa.ForeignKey("interview_turns.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("message_type", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("client_message_id", sa.String(64), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("role IN ('user', 'assistant', 'system')", name="ck_interview_chat_role"),
        sa.CheckConstraint(
            "message_type IN ('GREETING', 'MAIN_QUESTION', 'CLARIFY', 'PROBE', 'CANDIDATE_ANSWER', 'ACKNOWLEDGMENT', 'WRAP_UP')",
            name="ck_interview_chat_message_type",
        ),
        sa.UniqueConstraint("session_id", "sequence", name="uq_interview_chat_sequence"),
    )

    op.create_index(
        "ix_interview_chat_session_seq",
        "interview_chat_messages",
        ["session_id", "sequence"],
    )
    op.create_index(
        "ix_interview_chat_turn_id",
        "interview_chat_messages",
        ["turn_id"],
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_interview_chat_client_msg "
        "ON interview_chat_messages(session_id, client_message_id) "
        "WHERE client_message_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_interview_chat_client_msg")
    op.drop_index("ix_interview_chat_turn_id", table_name="interview_chat_messages")
    op.drop_index("ix_interview_chat_session_seq", table_name="interview_chat_messages")
    op.drop_table("interview_chat_messages")

    op.drop_index("ix_interview_sessions_experience_type", table_name="interview_sessions")
    op.drop_constraint("ck_interview_sessions_end_reason", "interview_sessions", type_="check")
    op.drop_constraint("ck_interview_sessions_experience_type", "interview_sessions", type_="check")
    op.drop_column("interview_sessions", "end_reason")
    op.drop_column("interview_sessions", "experience_type")
