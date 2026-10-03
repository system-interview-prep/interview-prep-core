"""Interview evaluations and turn-level STAR evaluations.

Revision ID: 20260926_0016
Revises: 20260925_0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260926_0016"
down_revision: str | None = "20260925_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Create interview_evaluations table
    op.create_table(
        "interview_evaluations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("interview_sessions.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("overall_score", sa.Numeric(4, 2), nullable=False),
        sa.Column("decision_recommendation", sa.String(20), nullable=False),
        sa.Column("competency_scores", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("recruiter_summary", sa.Text(), nullable=False),
        sa.Column("candidate_feedback", sa.Text(), nullable=False),
        sa.Column("next_round_topics", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("red_flags", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "decision_recommendation IN ('STRONG_PASS', 'PASS', 'CONSIDER', 'REJECT')",
            name="ck_interview_evaluations_decision",
        ),
    )

    op.create_index(
        "ix_interview_evaluations_session_id",
        "interview_evaluations",
        ["session_id"],
    )

    # 2. Create interview_turn_evaluations table
    op.create_table(
        "interview_turn_evaluations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "evaluation_id",
            sa.String(36),
            sa.ForeignKey("interview_evaluations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "turn_id",
            sa.String(36),
            sa.ForeignKey("interview_turns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("score", sa.Numeric(4, 2), nullable=False),
        sa.Column("star_analysis", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("evidence_quotes", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("strengths", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("weaknesses", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_index(
        "ix_interview_turn_evaluations_eval_id",
        "interview_turn_evaluations",
        ["evaluation_id"],
    )
    op.create_index(
        "ix_interview_turn_evaluations_turn_id",
        "interview_turn_evaluations",
        ["turn_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_interview_turn_evaluations_turn_id", table_name="interview_turn_evaluations")
    op.drop_index("ix_interview_turn_evaluations_eval_id", table_name="interview_turn_evaluations")
    op.drop_table("interview_turn_evaluations")

    op.drop_index("ix_interview_evaluations_session_id", table_name="interview_evaluations")
    op.drop_table("interview_evaluations")
