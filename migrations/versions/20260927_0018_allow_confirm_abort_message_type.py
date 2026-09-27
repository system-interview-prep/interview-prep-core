"""Allow CONFIRM_ABORT message type in interview_chat_messages.

Revision ID: 20260927_0018
Revises: 20260927_0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0018"
down_revision: str | None = "20260927_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_interview_chat_message_type", "interview_chat_messages", type_="check")
    op.create_check_constraint(
        "ck_interview_chat_message_type",
        "interview_chat_messages",
        "message_type IN ('GREETING', 'MAIN_QUESTION', 'CLARIFY', 'PROBE', 'CANDIDATE_ANSWER', 'ACKNOWLEDGMENT', 'WRAP_UP', 'CONFIRM_ABORT')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_interview_chat_message_type", "interview_chat_messages", type_="check")
    op.create_check_constraint(
        "ck_interview_chat_message_type",
        "interview_chat_messages",
        "message_type IN ('GREETING', 'MAIN_QUESTION', 'CLARIFY', 'PROBE', 'CANDIDATE_ANSWER', 'ACKNOWLEDGMENT', 'WRAP_UP')",
    )
