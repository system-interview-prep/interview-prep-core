from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import require_admin
from src.infrastructure.database import get_db

router = APIRouter(prefix="/admin/taxonomy", tags=["taxonomy"])


class TaxonomyVersionUpsert(BaseModel):
    version: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,80}$")
    priority: int = 0
    activate: bool = False


class TaxonomyConceptUpsert(BaseModel):
    label: str = Field(min_length=1, max_length=256)
    kind: str = Field(default="skill", pattern=r"^[a-z_]{2,64}$")
    aliases: list[str] = Field(default_factory=list)
    isActive: bool = True


@router.get("/active")
async def active_taxonomy(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
    version = (await db.execute(text("SELECT version, priority, published_at FROM taxonomy_versions WHERE is_active ORDER BY priority DESC, published_at DESC LIMIT 1"))).mappings().one_or_none()
    if not version:
        return {"version": None, "concepts": []}
    concepts = (await db.execute(text("SELECT concept_id, label, kind, is_active FROM taxonomy_concepts WHERE taxonomy_version = :version ORDER BY concept_id"), {"version": version["version"]})).mappings().all()
    return {"version": version["version"], "priority": version["priority"], "publishedAt": version["published_at"], "concepts": list(concepts)}


@router.put("/versions")
async def upsert_version(payload: TaxonomyVersionUpsert, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
    if payload.activate:
        await db.execute(text("UPDATE taxonomy_versions SET is_active = false WHERE is_active"))
    await db.execute(text("""
        INSERT INTO taxonomy_versions (version, priority, is_active)
        VALUES (:version, :priority, :active)
        ON CONFLICT (version) DO UPDATE SET priority = EXCLUDED.priority, is_active = EXCLUDED.is_active, published_at = now()
    """), {"version": payload.version, "priority": payload.priority, "active": payload.activate})
    await db.commit()
    return {"version": payload.version, "active": payload.activate, "priority": payload.priority}


@router.post("/versions/{version}/activate")
async def activate_version(version: str, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
    skill_count = (await db.execute(text("SELECT count(*) FROM taxonomy_concepts WHERE taxonomy_version = :version AND kind = 'skill' AND is_active"), {"version": version})).scalar_one()
    if not skill_count:
        raise HTTPException(status_code=422, detail="taxonomy version needs at least one active skill before activation")
    await db.execute(text("UPDATE taxonomy_versions SET is_active = false WHERE is_active"))
    result = await db.execute(text("UPDATE taxonomy_versions SET is_active = true, published_at = now() WHERE version = :version"), {"version": version})
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="taxonomy version not found")
    await db.commit()
    return {"version": version, "active": True}


@router.put("/versions/{version}/concepts/{concept_id}")
async def upsert_concept(version: str, concept_id: str, payload: TaxonomyConceptUpsert, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
    if not (await db.execute(text("SELECT 1 FROM taxonomy_versions WHERE version = :version"), {"version": version})).scalar_one_or_none():
        raise HTTPException(status_code=404, detail="taxonomy version not found")
    aliases = list(dict.fromkeys([payload.label, *(alias.strip() for alias in payload.aliases if alias.strip())]))
    await db.execute(text("""
        INSERT INTO taxonomy_concepts (taxonomy_version, concept_id, label, kind, is_active)
        VALUES (:version, :concept_id, :label, :kind, :active)
        ON CONFLICT (taxonomy_version, concept_id) DO UPDATE SET label = EXCLUDED.label, kind = EXCLUDED.kind, is_active = EXCLUDED.is_active
    """), {"version": version, "concept_id": concept_id, "label": payload.label, "kind": payload.kind, "active": payload.isActive})
    await db.execute(text("DELETE FROM taxonomy_aliases WHERE taxonomy_version = :version AND concept_id = :concept_id"), {"version": version, "concept_id": concept_id})
    for alias in aliases:
        await db.execute(text("INSERT INTO taxonomy_aliases (taxonomy_version, concept_id, alias) VALUES (:version, :concept_id, :alias)"), {"version": version, "concept_id": concept_id, "alias": alias})
    await db.commit()
    return {"version": version, "conceptId": concept_id, "aliases": aliases}
