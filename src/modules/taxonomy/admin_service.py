"""Taxonomy persistence and business rules; no HTTP concerns."""
import json
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.modules.taxonomy.schemas import TaxonomyConceptUpsert, TaxonomyRelationUpsert, TaxonomyVersionUpsert

class TaxonomyAdminService:
    def __init__(self, db: AsyncSession): self.db = db
    async def active(self):
        v=(await self.db.execute(text("SELECT version,priority,published_at FROM taxonomy_versions WHERE is_active ORDER BY priority DESC,published_at DESC LIMIT 1"))).mappings().one_or_none()
        if not v:return {"version":None,"concepts":[]}
        c=(await self.db.execute(text("SELECT concept_id,label,kind,description,metadata,is_active FROM taxonomy_concepts WHERE taxonomy_version=:v ORDER BY concept_id"),{"v":v["version"]})).mappings().all()
        return {"version":v["version"],"priority":v["priority"],"publishedAt":v["published_at"],"concepts":list(c)}
    async def upsert_version(self,p:TaxonomyVersionUpsert):
        if p.activate:await self.db.execute(text("UPDATE taxonomy_versions SET is_active=false WHERE is_active"))
        await self.db.execute(text("INSERT INTO taxonomy_versions(version,priority,is_active) VALUES(:version,:priority,:activate) ON CONFLICT(version) DO UPDATE SET priority=EXCLUDED.priority,is_active=EXCLUDED.is_active,published_at=now()"),p.model_dump())
        await self.db.commit();return {"version":p.version,"active":p.activate,"priority":p.priority}
    async def activate(self,v:str):
        if not await self.db.scalar(text("SELECT count(*) FROM taxonomy_concepts WHERE taxonomy_version=:v AND kind='skill' AND is_active"),{"v":v}):raise HTTPException(422,"Taxonomy version needs an active skill.")
        await self.db.execute(text("UPDATE taxonomy_versions SET is_active=false WHERE is_active"));r=await self.db.execute(text("UPDATE taxonomy_versions SET is_active=true,published_at=now() WHERE version=:v"),{"v":v})
        if not r.rowcount:raise HTTPException(404,"Taxonomy version not found.")
        await self.db.commit();return {"version":v,"active":True}
    async def upsert_concept(self,v:str,cid:str,p:TaxonomyConceptUpsert):
        if not await self.db.scalar(text("SELECT 1 FROM taxonomy_versions WHERE version=:v"),{"v":v}):raise HTTPException(404,"Taxonomy version not found.")
        aliases=list(dict.fromkeys([p.label,*(x.strip() for x in p.aliases if x.strip())]))
        await self.db.execute(text("INSERT INTO taxonomy_concepts(taxonomy_version,concept_id,label,kind,description,metadata,is_active) VALUES(:v,:id,:label,:kind,:description,CAST(:metadata AS jsonb),:active) ON CONFLICT(taxonomy_version,concept_id) DO UPDATE SET label=EXCLUDED.label,kind=EXCLUDED.kind,description=EXCLUDED.description,metadata=EXCLUDED.metadata,is_active=EXCLUDED.is_active"),{"v":v,"id":cid,"label":p.label,"kind":p.kind,"description":p.description,"metadata":json.dumps(p.metadata),"active":p.isActive})
        await self.db.execute(text("DELETE FROM taxonomy_aliases WHERE taxonomy_version=:v AND concept_id=:id"),{"v":v,"id":cid})
        for a in aliases:await self.db.execute(text("INSERT INTO taxonomy_aliases(taxonomy_version,concept_id,alias) VALUES(:v,:id,:a)"),{"v":v,"id":cid,"a":a})
        await self.db.commit();return {"version":v,"conceptId":cid,"aliases":aliases}
    async def upsert_relation(self,v:str,p:TaxonomyRelationUpsert):
        if p.sourceConceptId==p.targetConceptId:raise HTTPException(422,"A taxonomy relation cannot point to itself.")
        rows=await self.db.execute(text("SELECT concept_id FROM taxonomy_concepts WHERE taxonomy_version=:v AND concept_id IN (:s,:t)"),{"v":v,"s":p.sourceConceptId,"t":p.targetConceptId})
        if len(rows.scalars().all())!=2:raise HTTPException(422,"Both taxonomy concepts must exist in this version.")
        await self.db.execute(text("INSERT INTO taxonomy_relations(taxonomy_version,source_concept_id,target_concept_id,relation_type,weight,is_required,status) VALUES(:v,:s,:t,:r,:w,:required,:status) ON CONFLICT(taxonomy_version,source_concept_id,target_concept_id,relation_type) DO UPDATE SET weight=EXCLUDED.weight,is_required=EXCLUDED.is_required,status=EXCLUDED.status"),{"v":v,"s":p.sourceConceptId,"t":p.targetConceptId,"r":p.relationType,"w":p.weight,"required":p.isRequired,"status":p.status})
        await self.db.commit();return {"version":v,**p.model_dump()}
