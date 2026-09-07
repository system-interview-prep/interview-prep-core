"""Enable ParadeDB BM25 indexes for CV and JD corpora.

Revision ID: 20260906_0002
Revises: 20260906_0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260906_0002"
down_revision: str | None = "20260906_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_search")
    op.execute(
        """
        CREATE INDEX user_cvs_bm25_idx
        ON user_cvs
        USING bm25 (id, (raw_text::pdb.simple))
        WITH (key_field = 'id')
        """
    )
    op.execute(
        """
        CREATE INDEX job_profiles_bm25_idx
        ON job_profiles
        USING bm25 (id, (raw_jd_text::pdb.simple), (description::pdb.simple))
        WITH (key_field = 'id')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS job_profiles_bm25_idx")
    op.execute("DROP INDEX IF EXISTS user_cvs_bm25_idx")
