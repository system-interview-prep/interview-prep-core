"""Add the P0 structured interview runtime foundation.

Revision ID: 20260925_0013
Revises: 20260924_0012

This migration extends the legacy interview_sessions table in place so existing
chat/voice/video rows remain readable while the new structured runtime gets a
stable CV/JD context, modality, locale, draft plan, competency targets, and
frozen turn container.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_0013"
down_revision: str | None = "20260924_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "interview_sessions",
        sa.Column(
            "resume_id",
            sa.String(36),
            sa.ForeignKey("user_cvs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "interview_sessions",
        sa.Column(
            "job_id",
            sa.String(36),
            sa.ForeignKey("job_descriptions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("interview_sessions", sa.Column("mode", sa.String(16), nullable=True))
    op.add_column("interview_sessions", sa.Column("locale", sa.String(35), nullable=True))
    op.add_column(
        "interview_sessions",
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="25"),
    )

    op.execute(
        """
        UPDATE interview_sessions
        SET mode = CASE
            WHEN lower(type) = 'voice' THEN 'voice'
            WHEN lower(type) = 'call' THEN 'video'
            ELSE 'text'
        END
        WHERE mode IS NULL
        """
    )
    op.execute(
        """
        UPDATE interview_sessions
        SET locale = CASE
            WHEN lower(language) LIKE 'vietnam%' OR lower(language) LIKE 'vi%' THEN 'vi-VN'
            ELSE 'en-US'
        END
        WHERE locale IS NULL
        """
    )
    op.alter_column("interview_sessions", "mode", nullable=False, server_default="text")
    op.alter_column("interview_sessions", "locale", nullable=False, server_default="en-US")
    op.create_check_constraint(
        "ck_interview_sessions_mode",
        "interview_sessions",
        "mode IN ('text', 'voice', 'video')",
    )
    op.create_check_constraint(
        "ck_interview_sessions_duration",
        "interview_sessions",
        "duration_minutes BETWEEN 5 AND 120",
    )
    op.create_index(
        "ix_interview_sessions_context",
        "interview_sessions",
        ["user_id", "resume_id", "job_id", "started_at"],
    )

    op.create_table(
        "interview_session_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("interview_sessions.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("schema_version", sa.String(32), nullable=False, server_default="1.0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="DRAFT"),
        sa.Column(
            "source_context",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'READY', 'LOCKED', 'FAILED')",
            name="ck_interview_session_plans_status",
        ),
    )
    op.create_index(
        "ix_interview_session_plans_session",
        "interview_session_plans",
        ["session_id"],
        unique=True,
    )

    op.create_table(
        "session_competency_targets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(36),
            sa.ForeignKey("interview_session_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("taxonomy_version", sa.String(80), nullable=False),
        sa.Column("concept_id", sa.String(256), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("importance", sa.Numeric(5, 4), nullable=False),
        sa.Column("target_question_count", sa.Integer(), nullable=False),
        sa.Column(
            "rationale",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "importance >= 0 AND importance <= 1",
            name="ck_session_competency_targets_importance",
        ),
        sa.CheckConstraint(
            "target_question_count >= 0",
            name="ck_session_competency_targets_question_count",
        ),
        sa.UniqueConstraint(
            "plan_id",
            "taxonomy_version",
            "concept_id",
            name="uq_session_competency_target",
        ),
    )
    op.create_index(
        "ix_session_competency_targets_plan",
        "session_competency_targets",
        ["plan_id"],
    )

    op.create_table(
        "interview_turns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("interview_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="PLANNED"),
        # Question-bank tables are bootstrap-created at application startup, so
        # P0 stores immutable IDs without cross-schema FKs. P2 validates them
        # before a turn is frozen.
        sa.Column("question_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rubric_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "question_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("answer_text", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('PLANNED', 'ASKED', 'ANSWERED', 'EVALUATED', 'SKIPPED')",
            name="ck_interview_turns_status",
        ),
        sa.CheckConstraint("turn_index >= 0", name="ck_interview_turns_index"),
        sa.UniqueConstraint("session_id", "turn_index", name="uq_interview_turn_order"),
    )
    op.create_index(
        "ix_interview_turns_session_status",
        "interview_turns",
        ["session_id", "status", "turn_index"],
    )


def downgrade() -> None:
    op.drop_index("ix_interview_turns_session_status", table_name="interview_turns")
    op.drop_table("interview_turns")

    op.drop_index(
        "ix_session_competency_targets_plan",
        table_name="session_competency_targets",
    )
    op.drop_table("session_competency_targets")

    op.drop_index(
        "ix_interview_session_plans_session",
        table_name="interview_session_plans",
    )
    op.drop_table("interview_session_plans")

    op.drop_index("ix_interview_sessions_context", table_name="interview_sessions")
    op.drop_constraint(
        "ck_interview_sessions_duration",
        "interview_sessions",
        type_="check",
    )
    op.drop_constraint(
        "ck_interview_sessions_mode",
        "interview_sessions",
        type_="check",
    )
    op.drop_column("interview_sessions", "duration_minutes")
    op.drop_column("interview_sessions", "locale")
    op.drop_column("interview_sessions", "mode")
    op.drop_column("interview_sessions", "job_id")
    op.drop_column("interview_sessions", "resume_id")
