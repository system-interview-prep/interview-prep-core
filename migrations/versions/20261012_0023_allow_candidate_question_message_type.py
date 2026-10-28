"""Allow CANDIDATE_QUESTION message type in interview_chat_messages.

Revision ID: 20261012_0023
Revises: 20261011_0022

The chat runtime stores a candidate's question to the interviewer ("rubric
chấm thế nào?") as CANDIDATE_QUESTION so it is not read as an answer, but the
check constraint did not list it and every such message failed with a 500.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261012_0023"
down_revision: str | None = "20261011_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_interview_chat_message_type", "interview_chat_messages", type_="check")
    op.create_check_constraint(
        "ck_interview_chat_message_type",
        "interview_chat_messages",
        "message_type IN ('GREETING', 'MAIN_QUESTION', 'CLARIFY', 'PROBE', 'CANDIDATE_ANSWER', "
        "'CANDIDATE_QUESTION', 'ACKNOWLEDGMENT', 'WRAP_UP', 'CONFIRM_ABORT')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE interview_chat_messages SET message_type = 'CLARIFY' "
        "WHERE message_type = 'CANDIDATE_QUESTION'"
    )
    op.drop_constraint("ck_interview_chat_message_type", "interview_chat_messages", type_="check")
    op.create_check_constraint(
        "ck_interview_chat_message_type",
        "interview_chat_messages",
        "message_type IN ('GREETING', 'MAIN_QUESTION', 'CLARIFY', 'PROBE', 'CANDIDATE_ANSWER', "
        "'ACKNOWLEDGMENT', 'WRAP_UP', 'CONFIRM_ABORT')",
    )
