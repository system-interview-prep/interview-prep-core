"""Allow CANDIDATE_LEFT end reason in interview_sessions.

A voice/video interview whose candidate left the room and did not come back
within the grace period is closed by the session reaper with this reason
(instead of HARD_TIMEOUT), so the admin board can flag it.

Revision ID: 20261020_0034
Revises: 20261020_0033

Downgrade remaps CANDIDATE_LEFT -> USER_ENDED (lossy) to satisfy the previous
check constraint.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261020_0034"
down_revision: str | None = "20261020_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WITH = (
    "end_reason IS NULL OR end_reason IN ('COMPLETED', 'USER_ENDED', 'TECHNICAL_FAILURE', "
    "'HARD_TIMEOUT', 'FAST_FAIL_TECH', 'CANDIDATE_LEFT')"
)
_WITHOUT = (
    "end_reason IS NULL OR end_reason IN ('COMPLETED', 'USER_ENDED', 'TECHNICAL_FAILURE', "
    "'HARD_TIMEOUT', 'FAST_FAIL_TECH')"
)


def upgrade() -> None:
    op.drop_constraint("ck_interview_sessions_end_reason", "interview_sessions", type_="check")
    op.create_check_constraint("ck_interview_sessions_end_reason", "interview_sessions", _WITH)


def downgrade() -> None:
    op.execute("UPDATE interview_sessions SET end_reason = 'USER_ENDED' WHERE end_reason = 'CANDIDATE_LEFT'")
    op.drop_constraint("ck_interview_sessions_end_reason", "interview_sessions", type_="check")
    op.create_check_constraint("ck_interview_sessions_end_reason", "interview_sessions", _WITHOUT)
