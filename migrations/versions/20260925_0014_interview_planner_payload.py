"""Persist the P1 deterministic interview plan payload.

Revision ID: 20260925_0014
Revises: 20260925_0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_0014"
down_revision: str | None = "20260925_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "interview_session_plans",
        sa.Column(
            "plan_payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $p1$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM interview_session_plans
                WHERE plan_payload <> '{}'::jsonb
            )
            THEN
                RAISE EXCEPTION
                    'Refusing downgrade 20260925_0014: P1 plan payload data exists. '
                    'Export or migrate that data before downgrading.';
            END IF;
        END
        $p1$;
        """
    )
    op.drop_column("interview_session_plans", "plan_payload")
