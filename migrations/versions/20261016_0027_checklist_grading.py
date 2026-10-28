"""Checklist grading: per-point verdicts on turn evaluations and a verdict cache.

Revision ID: 20261016_0027
Revises: 20261016_0026
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261016_0027"
down_revision: str | None = "20261016_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE interview_turn_evaluations
          ADD COLUMN rubric_points JSONB NOT NULL DEFAULT '[]'::jsonb;
        CREATE TABLE grading_verdict_cache (
          cache_key CHAR(64) PRIMARY KEY,
          verdict JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE grading_verdict_cache;
        ALTER TABLE interview_turn_evaluations DROP COLUMN rubric_points;
        """
    )
