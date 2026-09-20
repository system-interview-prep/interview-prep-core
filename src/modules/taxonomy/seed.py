"""Idempotent seed for the built-in career taxonomy."""

import json
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.taxonomy.career import _RULES, TAXONOMY_VERSION, career_taxonomy


def _kind(dimension: str) -> str:
    return {
        "domain": "domain",
        "occupation": "occupation",
        "specialization": "competency",
        "skill": "skill",
    }[dimension]


async def seed_default_taxonomy(session_factory: Callable[[], AsyncSession]) -> bool:
    """Ensure the built-in taxonomy exists without overwriting admin edits."""
    async with session_factory() as session:
        existing = await session.scalar(
            text("SELECT 1 FROM taxonomy_versions WHERE version = :version"),
            {"version": TAXONOMY_VERSION},
        )
        if existing:
            return False
        await session.execute(
            text(
                "INSERT INTO taxonomy_versions(version, priority, is_active) "
                "VALUES (:version, 0, false)"
            ),
            {"version": TAXONOMY_VERSION},
        )
        nodes = career_taxonomy()
        concepts = {node.code: (node.label, node.dimension) for node in nodes}
        for rule in _RULES:
            for skill_id in rule.skill_ids:
                concepts.setdefault(
                    skill_id,
                    (skill_id.removeprefix("skill-").replace("-", " ").title(), "skill"),
                )
        for concept_id, (label, dimension) in concepts.items():
            await session.execute(
                text(
                    "INSERT INTO taxonomy_concepts "
                    "(taxonomy_version, concept_id, label, kind, metadata, is_active) "
                    "VALUES (:version, :concept_id, :label, :kind, CAST(:metadata AS jsonb), true)"
                ),
                {
                    "version": TAXONOMY_VERSION,
                    "concept_id": concept_id,
                    "label": label,
                    "kind": _kind(dimension),
                    "metadata": json.dumps({"seed": "career_taxonomy"}),
                },
            )
            await session.execute(
                text(
                    "INSERT INTO taxonomy_aliases(taxonomy_version, concept_id, alias) "
                    "VALUES (:version, :concept_id, :alias) ON CONFLICT DO NOTHING"
                ),
                {"version": TAXONOMY_VERSION, "concept_id": concept_id, "alias": label},
            )
        for node in nodes:
            if node.parent_code:
                await session.execute(
                    text(
                        "INSERT INTO taxonomy_relations "
                        "(taxonomy_version, source_concept_id, target_concept_id, relation_type) "
                        "VALUES (:version, :source, :target, 'PARENT_OF') ON CONFLICT DO NOTHING"
                    ),
                    {"version": TAXONOMY_VERSION, "source": node.parent_code, "target": node.code},
                )
        await session.execute(
            text(
                "UPDATE taxonomy_versions SET is_active = true, published_at = now() "
                "WHERE version = :version"
            ),
            {"version": TAXONOMY_VERSION},
        )
        await session.commit()
    return True
