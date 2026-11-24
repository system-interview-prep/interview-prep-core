"""Fold diacritics in the BM25 indexes so both halves agree on a token.

Revision ID: 20261114_0041
Revises: c946667cf20b

The query half has always arrived here without diacritics: ``tokenize_text``
NFKD-normalizes and drops combining marks, so a posting asking for "kinh
nghiệm" reaches the index as "kinh nghiem". The index half kept them, because
``pdb.simple`` lowercases and nothing more. The two halves therefore never met
on any Vietnamese word carrying a mark: measured on the two Vietnamese CVs in
the corpus, 64.4% and 68.1% of their words could not be retrieved at all.

``ascii_folding`` makes the index tokenize both the stored text and the query
the same way, so "nghiem", "nghiệm" and "NGHIỆM" all reach the same term. It
also folds "đ" to "d", which is why ``tokenize_text`` now folds it too rather
than letting the ASCII token pattern cut the word in half.

Rebuilding the index changes the sparse score of every pair, so this belongs
with a baseline recomputation and not with a measurement already in flight.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261114_0041"
down_revision: str | None = "c946667cf20b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS user_cvs_bm25_idx")
    op.execute(
        """
        CREATE INDEX user_cvs_bm25_idx
        ON user_cvs
        USING bm25 (id, (raw_text::pdb.simple('ascii_folding=true')))
        WITH (key_field = 'id')
        """
    )
    op.execute("DROP INDEX IF EXISTS job_descriptions_bm25_idx")
    op.execute(
        """
        CREATE INDEX job_descriptions_bm25_idx
        ON job_descriptions
        USING bm25 (
            id,
            (raw_text::pdb.simple('ascii_folding=true')),
            (description::pdb.simple('ascii_folding=true'))
        )
        WITH (key_field = 'id')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS user_cvs_bm25_idx")
    op.execute(
        """
        CREATE INDEX user_cvs_bm25_idx
        ON user_cvs
        USING bm25 (id, (raw_text::pdb.simple))
        WITH (key_field = 'id')
        """
    )
    op.execute("DROP INDEX IF EXISTS job_descriptions_bm25_idx")
    op.execute(
        """
        CREATE INDEX job_descriptions_bm25_idx
        ON job_descriptions
        USING bm25 (id, (raw_text::pdb.simple), (description::pdb.simple))
        WITH (key_field = 'id')
        """
    )
