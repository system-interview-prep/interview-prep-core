"""Add runtime metadata to interview sessions.

Revision ID: 20260927_0017
Revises: 20260926_0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260927_0017"
down_revision: str | None = "20260926_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns("interview_sessions")]
    if "metadata" not in columns:
        op.add_column(
            "interview_sessions",
            sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        )
    else:
        op.execute("UPDATE interview_sessions SET metadata = '{}' WHERE metadata IS NULL")
        op.alter_column("interview_sessions", "metadata", nullable=False, server_default="{}")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns("interview_sessions")]
    if "metadata" in columns:
        op.drop_column("interview_sessions", "metadata")

