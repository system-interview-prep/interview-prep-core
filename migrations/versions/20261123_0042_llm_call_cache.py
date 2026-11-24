"""Cache answers of structured model calls by content hash, in the database.

The judges, the CV text repair and the evidence search answer one distinct
input once.  Until now the answers lived as files under models_cache/, which
a host without a persistent disk loses on every deploy and which two
instances cannot share.

Revision ID: 20261123_0042
Revises: 20261114_0041
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261123_0042"
down_revision: str | None = "20261114_0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE llm_call_cache (
          -- sha256 of prompt version + model + instructions + input (see src/modules/ai/cache_store.py)
          cache_key CHAR(64) PRIMARY KEY,
          -- judge | gate | numeric | text_repair | evidence_search: for clearing one reader's answers
          purpose VARCHAR(64) NOT NULL,
          -- the model's answer, verbatim; its shape is checked again on every read
          body TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_llm_call_cache_purpose ON llm_call_cache(purpose);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE llm_call_cache;")
