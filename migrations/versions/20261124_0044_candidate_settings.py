"""Persist candidate preferences separately from user profile data.

Revision ID: 20261124_0044
Revises: 20261124_0043
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261124_0044"
down_revision: str | None = "20261124_0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candidate_settings",
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ai_voice", sa.String(length=100), nullable=False, server_default="vi-north-male"),
        sa.Column(
            "speech_rate",
            sa.Numeric(2, 1),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column("auto_camera_webrtc", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("ai_tone", sa.String(length=20), nullable=False, server_default="standard"),
        sa.Column("notify_weekly_reminder", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_job_match", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_progress_report", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_sound_fx", sa.Boolean(), nullable=False, server_default=sa.true()),
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
        sa.CheckConstraint("speech_rate IN (0.8, 1.0, 1.2)", name="ck_candidate_settings_speech_rate"),
        sa.CheckConstraint(
            "ai_tone IN ('supportive', 'standard', 'strict')",
            name="ck_candidate_settings_ai_tone",
        ),
    )

    op.execute(
        sa.text(
            "INSERT INTO candidate_settings (user_id) SELECT id FROM users "
            "ON CONFLICT (user_id) DO NOTHING"
        )
    )


def downgrade() -> None:
    op.drop_table("candidate_settings")
