"""Remove obsolete local BERT vector columns from existing databases.

Revision ID: 20260908_0004
Revises: 20260908_0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260908_0004"
down_revision: str | None = "20260908_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table_name in ("job_descriptions_vector", "cv_profiles_vector"):
        op.execute(f"ALTER TABLE {table_name} DROP COLUMN IF EXISTS bert_vector")
        op.execute(f"ALTER TABLE {table_name} DROP COLUMN IF EXISTS distilbert_vector")


def downgrade() -> None:
    for table_name in ("job_descriptions_vector", "cv_profiles_vector"):
        op.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS bert_vector vector(768)")
        op.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS distilbert_vector vector(768)")
