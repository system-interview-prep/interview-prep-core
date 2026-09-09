"""Harden authentication schema and add onboarding credits.

Revision ID: 20260909_0006
Revises: 20260908_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260909_0006"
down_revision: str | None = "20260908_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("users", "password", new_column_name="password_hash")
    op.alter_column("users", "picture", new_column_name="avatar_url")
    op.add_column("users", sa.Column("phone", sa.String(32), nullable=True))
    op.add_column("users", sa.Column("google_id", sa.String(255), nullable=True))
    op.add_column(
        "users",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_users_google_id", "users", ["google_id"], unique=False)
    op.create_check_constraint(
        "ck_users_role",
        "users",
        "role IN ('CANDIDATE', 'ADMIN')",
    )

    op.create_table(
        "user_credits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("cv_scans_remaining", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("voice_mock_remaining", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("plan_tier", sa.String(32), nullable=False, server_default="FREE"),
        sa.Column("plan_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "plan_tier IN ('FREE', 'STARTER', 'CAREER_PRO', 'EXECUTIVE')",
            name="ck_user_credits_plan_tier",
        ),
        sa.CheckConstraint("cv_scans_remaining >= 0", name="ck_user_credits_cv_scans_nonnegative"),
        sa.CheckConstraint("voice_mock_remaining >= 0", name="ck_user_credits_voice_mock_nonnegative"),
    )
    op.execute(
        "INSERT INTO user_credits (id, user_id, cv_scans_remaining, voice_mock_remaining, plan_tier) "
        "SELECT gen_random_uuid()::text, id, 3, 1, 'FREE' FROM users"
    )


def downgrade() -> None:
    op.drop_table("user_credits")
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.drop_index("ix_users_google_id", table_name="users")
    op.drop_column("users", "is_active")
    op.drop_column("users", "google_id")
    op.drop_column("users", "phone")
    op.alter_column("users", "avatar_url", new_column_name="picture")
    op.alter_column("users", "password_hash", new_column_name="password")
