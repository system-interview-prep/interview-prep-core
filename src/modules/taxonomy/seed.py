"""Idempotent seed for the built-in career taxonomy and the shared skill catalogue."""

import json
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.taxonomy.career import TAXONOMY_VERSION, career_taxonomy
from src.modules.taxonomy.skill_catalog import SKILL_CATALOG


def _kind(dimension: str) -> str:
    return {
        "domain": "domain",
        "occupation": "occupation",
        "specialization": "competency",
        "skill": "skill",
    }[dimension]


async def seed_default_taxonomy(session_factory: Callable[[], AsyncSession]) -> bool:
    """Ensure the built-in taxonomy exists and carries every catalogue skill.

    Every statement is insert-if-missing, so concepts, labels and aliases an
    admin edited in the database are never overwritten; a catalogue skill
    added in code reaches an existing database on the next start.
    Returns True when the taxonomy version was created by this call.
    """
    async with session_factory() as session:
        existing = await session.scalar(
            text("SELECT 1 FROM taxonomy_versions WHERE version = :version"),
            {"version": TAXONOMY_VERSION},
        )
        if not existing:
            await session.execute(
                text(
                    "INSERT INTO taxonomy_versions(version, priority, is_active) VALUES (:version, 10, true)"
                ),
                {"version": TAXONOMY_VERSION},
            )

        nodes = career_taxonomy()
        concepts = {node.code: (node.label, _kind(node.dimension)) for node in nodes}
        concepts.update({concept_id: (entry.label, "skill") for concept_id, entry in SKILL_CATALOG.items()})
        for concept_id, (label, kind) in concepts.items():
            await session.execute(
                text(
                    "INSERT INTO taxonomy_concepts "
                    "(taxonomy_version, concept_id, label, kind, metadata, is_active) "
                    "VALUES (:version, :concept_id, :label, :kind, CAST(:metadata AS jsonb), true) "
                    "ON CONFLICT (taxonomy_version, concept_id) DO NOTHING"
                ),
                {
                    "version": TAXONOMY_VERSION,
                    "concept_id": concept_id,
                    "label": label,
                    "kind": kind,
                    "metadata": json.dumps(
                        {"seed": "skill_catalog" if kind == "skill" else "career_taxonomy"}
                    ),
                },
            )
            entry = SKILL_CATALOG.get(concept_id)
            for alias in (label, *(entry.aliases if entry else ())):
                await session.execute(
                    text(
                        "INSERT INTO taxonomy_aliases(taxonomy_version, concept_id, alias) "
                        "VALUES (:version, :concept_id, :alias) ON CONFLICT DO NOTHING"
                    ),
                    {"version": TAXONOMY_VERSION, "concept_id": concept_id, "alias": alias},
                )

        relations = [(node.parent_code, node.code, "PARENT_OF") for node in nodes if node.parent_code]
        for concept_id, entry in SKILL_CATALOG.items():
            # A narrower skill specialises a broader one (MySQL -> SQL); a
            # specialisation requires the skills that signal it.
            relations += [(concept_id, broader, "SPECIALIZES") for broader in entry.broader]
            relations += [(role, concept_id, "REQUIRES_SKILL") for role in entry.roles]
        for source, target, relation_type in relations:
            await session.execute(
                text(
                    "INSERT INTO taxonomy_relations "
                    "(taxonomy_version, source_concept_id, target_concept_id, relation_type) "
                    "VALUES (:version, :source, :target, :relation_type) ON CONFLICT DO NOTHING"
                ),
                {
                    "version": TAXONOMY_VERSION,
                    "source": source,
                    "target": target,
                    "relation_type": relation_type,
                },
            )

        if not existing:
            await session.execute(
                text(
                    "UPDATE taxonomy_versions SET is_active = true, published_at = now() "
                    "WHERE version = :version"
                ),
                {"version": TAXONOMY_VERSION},
            )
        await session.commit()
    return not existing
