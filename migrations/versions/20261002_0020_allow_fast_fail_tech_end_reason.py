"""Allow FAST_FAIL_TECH end reason in interview_sessions.

Revision ID: 20261002_0020
Revises: 20260930_0019

LƯU Ý VỀ MIGRATION DOWNGRADE:
Constraint cũ tại revision 20260930_0019 chỉ cho phép:
  ('COMPLETED', 'USER_ENDED', 'TECHNICAL_FAILURE', 'HARD_TIMEOUT')
Do đó, khi downgrade, hệ thống KHÔNG THỂ giữ nguyên mã 'FAST_FAIL_TECH'.
Hàm downgrade thực hiện remap 'FAST_FAIL_TECH' -> 'COMPLETED' (giá trị fallback
lịch sử mà runtime từng áp dụng trước khi fix). Đây là giải pháp kỹ thuật bắt buộc
để thỏa mãn DB check constraint cũ, mang tính mất mát dữ liệu (lossy) và
TUYỆT ĐỐI KHÔNG BẢO TOÀN NGỮ NGHĨA kinh doanh của phiên fast-fail kỹ thuật.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261002_0020"
down_revision: str | None = "20260930_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_interview_sessions_end_reason", "interview_sessions", type_="check")
    op.create_check_constraint(
        "ck_interview_sessions_end_reason",
        "interview_sessions",
        "end_reason IS NULL OR end_reason IN ('COMPLETED', 'USER_ENDED', 'TECHNICAL_FAILURE', 'HARD_TIMEOUT', 'FAST_FAIL_TECH')",
    )


def downgrade() -> None:
    # Lossy technical fallback: Remap FAST_FAIL_TECH -> COMPLETED to satisfy legacy check constraint.
    # Note: This does NOT preserve fast-fail business semantics.
    op.execute(
        "UPDATE interview_sessions SET end_reason = 'COMPLETED' WHERE end_reason = 'FAST_FAIL_TECH'"
    )
    op.drop_constraint("ck_interview_sessions_end_reason", "interview_sessions", type_="check")
    op.create_check_constraint(
        "ck_interview_sessions_end_reason",
        "interview_sessions",
        "end_reason IS NULL OR end_reason IN ('COMPLETED', 'USER_ENDED', 'TECHNICAL_FAILURE', 'HARD_TIMEOUT')",
    )
