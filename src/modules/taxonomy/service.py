from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class TaxonomySnapshot:
    version: str
    skills: dict[str, tuple[str, tuple[str, ...]]]


async def load_active_skill_taxonomy(db: AsyncSession) -> TaxonomySnapshot:
    version = (await db.execute(text("SELECT version FROM taxonomy_versions WHERE is_active ORDER BY priority DESC, published_at DESC LIMIT 1"))).scalar_one_or_none()
    if not version:
        raise RuntimeError("no active taxonomy version")
    rows = (await db.execute(text("""
        SELECT c.concept_id, c.label, array_agg(a.alias ORDER BY a.alias) AS aliases
        FROM taxonomy_concepts c JOIN taxonomy_aliases a USING (taxonomy_version, concept_id)
        WHERE c.taxonomy_version = :version AND c.kind = 'skill' AND c.is_active
        GROUP BY c.concept_id, c.label
    """), {"version": version})).mappings().all()
    return TaxonomySnapshot(str(version), {row["concept_id"]: (row["label"], tuple(row["aliases"])) for row in rows})
