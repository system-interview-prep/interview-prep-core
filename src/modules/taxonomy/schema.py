"""Clean-slate PostgreSQL extensions for the shared taxonomy."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def create_taxonomy_schema(engine: AsyncEngine) -> None:
    """Make the legacy skill catalogue capable of serving every module.

    This project is explicitly clean-slate.  The base Alembic revision creates
    the three original taxonomy tables; this bootstrap extends them without a
    compatibility migration for an old production database.
    """

    async with engine.begin() as connection:
        await connection.execute(
            text("ALTER TABLE taxonomy_concepts ADD COLUMN IF NOT EXISTS description TEXT")
        )
        await connection.execute(
            text(
                "ALTER TABLE taxonomy_concepts ADD COLUMN IF NOT EXISTS metadata JSONB "
                "NOT NULL DEFAULT '{}'::jsonb"
            )
        )
        await connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS taxonomy_relations (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    taxonomy_version TEXT NOT NULL REFERENCES taxonomy_versions(version) ON DELETE CASCADE,
                    source_concept_id TEXT NOT NULL,
                    target_concept_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    weight NUMERIC(5,4),
                    is_required BOOLEAN,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (taxonomy_version, source_concept_id, target_concept_id, relation_type),
                    FOREIGN KEY (taxonomy_version, source_concept_id)
                      REFERENCES taxonomy_concepts(taxonomy_version, concept_id) ON DELETE CASCADE,
                    FOREIGN KEY (taxonomy_version, target_concept_id)
                      REFERENCES taxonomy_concepts(taxonomy_version, concept_id) ON DELETE CASCADE,
                    CHECK (source_concept_id <> target_concept_id),
                    CHECK (weight IS NULL OR weight BETWEEN 0 AND 1)
                )
                """
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_taxonomy_relations_source "
                "ON taxonomy_relations(taxonomy_version, source_concept_id, relation_type)"
            )
        )
