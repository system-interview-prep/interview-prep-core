"""Create the PostgreSQL primary datastore schema.

Revision ID: 20260906_0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260906_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password", sa.Text(), nullable=True),
        sa.Column("name", sa.String(255), nullable=False, server_default=""),
        sa.Column("provider", sa.String(32), nullable=False, server_default="local"),
        sa.Column("dob", sa.Date(), nullable=True),
        sa.Column("picture", sa.Text(), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_users_email_lower", "users", [sa.text("lower(email)")], unique=True)
    op.create_table(
        "user_role_assignments",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", sa.String(32), primary_key=True),
        sa.Column("assigned_by", sa.String(36), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "user_cvs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("parse_source", sa.String(64), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("parsed_data", postgresql.JSONB(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("user_id", "checksum", name="uq_user_cvs_user_checksum"),
    )
    op.create_index("ix_user_cvs_user_created", "user_cvs", ["user_id", "created_at"])

    op.create_table(
        "job_descriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("item_type", sa.String(32), nullable=False, server_default="JOB_DESCRIPTION"),
        sa.Column("title", sa.String(512), nullable=False, server_default=""),
        sa.Column("keywords", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("requirements", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="DRAFT"),
        sa.Column("search_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("filename", sa.String(512), nullable=True),
        sa.Column("content_type", sa.String(255), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("checksum", sa.String(64), nullable=True),
        sa.Column("parse_source", sa.String(64), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("structured_data", postgresql.JSONB(), nullable=True),
        sa.Column("extracted_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("extraction_version", sa.String(32), nullable=True),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_job_descriptions_status_created", "job_descriptions", ["status", "created_at"])
    op.create_index(
        "ix_job_descriptions_search",
        "job_descriptions",
        [sa.text("to_tsvector('simple', search_text)")],
        postgresql_using="gin",
    )

    op.create_table(
        "interview_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("language", sa.String(64), nullable=False, server_default="English"),
        sa.Column("status", sa.String(32), nullable=False, server_default="Open"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_interview_sessions_user_started", "interview_sessions", ["user_id", "started_at"])

    for table_name in ("chat_text_messages", "chat_voice_messages"):
        op.create_table(
            table_name,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "session_id",
                sa.String(36),
                sa.ForeignKey("interview_sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("role", sa.String(32), nullable=False),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index(f"ix_{table_name}_session_created", table_name, ["session_id", "created_at"])

    op.create_table(
        "video_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("interview_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_video_calls_user_started", "video_calls", ["user_id", "started_at"])
    op.create_index("ix_video_calls_session_started", "video_calls", ["session_id", "started_at"])

    op.create_table(
        "scoring_history",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("interview_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_scoring_history_user_created", "scoring_history", ["user_id", "created_at"])

    op.execute("""
        CREATE TABLE job_descriptions_vector (
            job_description_id VARCHAR(255) PRIMARY KEY REFERENCES job_descriptions(id) ON DELETE CASCADE,
            job_description_vector vector(1024),
            checksum VARCHAR(64), version INTEGER NOT NULL DEFAULT 1, job_text TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE cv_profiles_vector (
            cv_id VARCHAR(255) PRIMARY KEY REFERENCES user_cvs(id) ON DELETE CASCADE,
            cv_vector vector(1024),
            checksum VARCHAR(64), version INTEGER NOT NULL DEFAULT 1, cv_text TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

def downgrade() -> None:
    for table_name in (
        "cv_profiles_vector",
        "job_descriptions_vector",
        "scoring_history",
        "video_calls",
        "chat_voice_messages",
        "chat_text_messages",
        "interview_sessions",
        "job_descriptions",
        "user_cvs",
        "users",
    ):
        op.drop_table(table_name)
