"""Add explicit AI skill concepts and aliases to the built-in career taxonomy.

Revision ID: 20260922_0010
Revises: 20260921_0009
Create Date: 2026-09-22 10:00:00.000000
"""

from alembic import op

revision = "20260922_0010"
down_revision = "20260921_0009"
branch_labels = None
depends_on = None

_VERSION = "internal-career-2026.1"
_SKILLS = (
    ("skill-python", "Python", ("python",)),
    ("skill-artificial-intelligence", "Artificial Intelligence", ("artificial intelligence", "ai")),
    ("skill-machine-learning", "Machine Learning", ("machine learning", "ml")),
    (
        "skill-natural-language-processing",
        "Natural Language Processing",
        ("natural language processing", "nlp"),
    ),
    ("skill-generative-ai", "Generative AI", ("generative ai", "genai", "gen ai")),
    (
        "skill-large-language-models",
        "Large Language Models",
        ("large language models", "large language model", "llms", "llm"),
    ),
    (
        "skill-retrieval-augmented-generation",
        "Retrieval-Augmented Generation",
        ("retrieval-augmented generation", "retrieval augmented generation", "rag"),
    ),
    ("skill-langgraph", "LangGraph", ("langgraph",)),
    ("skill-gemini", "Gemini", ("gemini", "google gemini")),
    ("skill-bm25", "BM25", ("bm25",)),
    ("skill-tf-idf", "TF-IDF", ("tf-idf", "tf idf")),
    ("skill-reciprocal-rank-fusion", "Reciprocal Rank Fusion", ("reciprocal rank fusion", "rrf")),
    ("skill-named-entity-recognition", "Named Entity Recognition", ("named entity recognition", "ner")),
    ("skill-semantic-search", "Semantic Search", ("semantic search",)),
)


def upgrade() -> None:
    # Revision 0007 originally created the taxonomy table without the
    # extensibility fields subsequently used by the taxonomy services.  This
    # migration must add ``metadata`` before its seed inserts: application
    # startup happens only *after* Alembic completes, so the runtime schema
    # bootstrap cannot make this insert safe.
    op.execute(
        "ALTER TABLE taxonomy_concepts "
        "ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb"
    )

    for concept_id, label, aliases in _SKILLS:
        op.execute(
            "INSERT INTO taxonomy_concepts "
            "(taxonomy_version, concept_id, label, kind, metadata, is_active) "
            f"SELECT '{_VERSION}', '{concept_id}', '{label}', 'skill', "
            '\'{"seed": "career_taxonomy"}\'::jsonb, true '
            f"WHERE EXISTS (SELECT 1 FROM taxonomy_versions WHERE version = '{_VERSION}') "
            "ON CONFLICT (taxonomy_version, concept_id) DO NOTHING"
        )
        for alias in aliases:
            escaped_alias = alias.replace("'", "''")
            op.execute(
                "INSERT INTO taxonomy_aliases (taxonomy_version, concept_id, alias) "
                f"SELECT '{_VERSION}', '{concept_id}', '{escaped_alias}' "
                "WHERE EXISTS (SELECT 1 FROM taxonomy_concepts "
                f"WHERE taxonomy_version = '{_VERSION}' AND concept_id = '{concept_id}') "
                "ON CONFLICT DO NOTHING"
            )


def downgrade() -> None:
    # Additive taxonomy data may have been referenced by parsed documents after
    # deployment. Preserve it on downgrade rather than invalidating those refs.
    pass
