"""Taxonomy persistence and business rules; no HTTP concerns."""
import json
import io
from openpyxl import Workbook, load_workbook
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.modules.taxonomy.schemas import TaxonomyConceptUpsert, TaxonomyRelationUpsert, TaxonomyVersionUpsert

class TaxonomyAdminService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def active(self):
        v=(await self.db.execute(text("SELECT version,priority,published_at FROM taxonomy_versions WHERE is_active ORDER BY priority DESC,published_at DESC LIMIT 1"))).mappings().one_or_none()
        if not v:
            return {"version":None,"concepts":[]}
        c=(await self.db.execute(text("""
            SELECT concept_id, label, kind, description, metadata, is_active,
              COALESCE(
                (SELECT json_agg(alias ORDER BY alias)
                 FROM taxonomy_aliases
                 WHERE taxonomy_version = :v AND concept_id = tc.concept_id),
                '[]'::json
              ) AS aliases
            FROM taxonomy_concepts AS tc
            WHERE taxonomy_version = :v
            ORDER BY concept_id
        """),{"v":v["version"]})).mappings().all()
        return {
            "version": v["version"],
            "priority": v["priority"],
            "publishedAt": v["published_at"],
            "concepts": [dict(concept) for concept in c],
        }

    async def versions(self) -> list[dict]:
        result = await self.db.execute(
            text("SELECT version, priority, is_active, published_at FROM taxonomy_versions ORDER BY priority DESC, version DESC")
        )
        return [
            {"version": row["version"], "priority": row["priority"], "isActive": row["is_active"], "publishedAt": row["published_at"]}
            for row in result.mappings().all()
        ]

    async def clone_version(self, source: str, target: str) -> int:
        exists = await self.db.scalar(text("SELECT 1 FROM taxonomy_versions WHERE version=:version"), {"version": source})
        if not exists:
            raise HTTPException(404, "Source taxonomy version not found.")
        await self.db.execute(text("INSERT INTO taxonomy_versions(version, priority, is_active) SELECT :target, priority, false FROM taxonomy_versions WHERE version=:source ON CONFLICT DO NOTHING"), {"source": source, "target": target})
        await self.db.execute(text("INSERT INTO taxonomy_concepts(taxonomy_version, concept_id, label, kind, description, metadata, is_active) SELECT :target, concept_id, label, kind, description, metadata, is_active FROM taxonomy_concepts WHERE taxonomy_version=:source ON CONFLICT DO NOTHING"), {"source": source, "target": target})
        await self.db.execute(text("INSERT INTO taxonomy_aliases(taxonomy_version, concept_id, alias) SELECT :target, concept_id, alias FROM taxonomy_aliases WHERE taxonomy_version=:source ON CONFLICT DO NOTHING"), {"source": source, "target": target})
        await self.db.execute(text("INSERT INTO taxonomy_relations(taxonomy_version, source_concept_id, target_concept_id, relation_type, weight, is_required, status) SELECT :target, source_concept_id, target_concept_id, relation_type, weight, is_required, status FROM taxonomy_relations WHERE taxonomy_version=:source ON CONFLICT DO NOTHING"), {"source": source, "target": target})
        count = await self.db.scalar(text("SELECT count(*) FROM taxonomy_concepts WHERE taxonomy_version=:version"), {"version": target})
        await self.db.commit()
        return int(count or 0)

    async def export_xlsx(self, version: str) -> bytes:
        result = await self.db.execute(
            text("SELECT concept_id, label, kind, description, is_active FROM taxonomy_concepts WHERE taxonomy_version=:version ORDER BY concept_id"),
            {"version": version},
        )
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Taxonomy"
        sheet.append(("concept_id", "label", "kind", "description", "is_active"))
        for row in result.mappings().all():
            sheet.append((row["concept_id"], row["label"], row["kind"], row["description"] or "", row["is_active"]))
        stream = io.BytesIO()
        workbook.save(stream)
        return stream.getvalue()

    async def import_xlsx(self, version: str, content: bytes) -> int:
        try:
            sheet = load_workbook(io.BytesIO(content), read_only=True, data_only=True).active
            values = list(sheet.values)
            headers = [str(value or "").strip() for value in (values[0] if values else ())]
            rows = [dict(zip(headers, row, strict=False)) for row in values[1:]]
        except Exception as exc:
            raise HTTPException(422, "Invalid taxonomy XLSX.") from exc
        required = {"concept_id", "label", "kind", "description", "is_active"}
        if not rows or not required.issubset(rows[0]):
            raise HTTPException(422, "Taxonomy XLSX has an invalid header or no rows.")
        await self.db.execute(
            text("INSERT INTO taxonomy_versions(version, priority, is_active) VALUES(:version, 0, false) ON CONFLICT DO NOTHING"),
            {"version": version},
        )
        for row in rows:
            if not row["concept_id"].strip() or not row["label"].strip() or row["kind"] not in {"domain", "occupation", "job_family", "job_role", "skill", "competency"}:
                raise HTTPException(422, "Taxonomy CSV contains an invalid concept.")
            source = str(row.get("source") or "").strip() or "xlsx"
            source_ref = str(row.get("source_ref") or "").strip() or None
            metadata = {"source": source}
            if source_ref:
                metadata["source_ref"] = source_ref
            await self.db.execute(
                text(
                    "INSERT INTO taxonomy_concepts(taxonomy_version, concept_id, label, kind, description, metadata, is_active) "
                    "VALUES(:version, :concept_id, :label, :kind, :description, CAST(:metadata AS jsonb), :is_active) "
                    "ON CONFLICT(taxonomy_version, concept_id) DO UPDATE SET label=EXCLUDED.label, kind=EXCLUDED.kind, description=EXCLUDED.description, metadata=EXCLUDED.metadata, is_active=EXCLUDED.is_active"
                ),
                {"version": version, "concept_id": row["concept_id"].strip(), "label": row["label"].strip(), "kind": row["kind"], "description": str(row["description"] or "").strip() or None, "metadata": json.dumps(metadata), "is_active": str(row["is_active"]).lower() in {"true", "1", "yes"}},
            )
            aliases = [row["label"].strip()]
            aliases.extend(
                item.strip()
                for item in str(row.get("aliases") or "").replace(",", "|").split("|")
                if item.strip()
            )
            for alias in dict.fromkeys(aliases):
                await self.db.execute(
                    text(
                        "INSERT INTO taxonomy_aliases(taxonomy_version, concept_id, alias) "
                        "VALUES(:version, :concept_id, :alias) ON CONFLICT DO NOTHING"
                    ),
                    {"version": version, "concept_id": row["concept_id"].strip(), "alias": alias},
                )
        await self.db.commit()
        return len(rows)
    async def upsert_version(self,p:TaxonomyVersionUpsert):
        if p.activate:
            await self._ensure_activatable(p.version)
            await self.db.execute(text("UPDATE taxonomy_versions SET is_active=false WHERE is_active"))
        await self.db.execute(text("INSERT INTO taxonomy_versions(version,priority,is_active) VALUES(:version,:priority,:activate) ON CONFLICT(version) DO UPDATE SET priority=EXCLUDED.priority,is_active=EXCLUDED.is_active,published_at=now()"),p.model_dump())
        await self.db.commit()
        return {"version":p.version,"active":p.activate,"priority":p.priority}
    async def delete_version(self, version: str) -> dict:
        is_active = await self.db.scalar(
            text("SELECT is_active FROM taxonomy_versions WHERE version=:version"),
            {"version": version},
        )
        if is_active is None:
            raise HTTPException(404, "Taxonomy version not found.")
        if is_active:
            raise HTTPException(422, "Cannot delete the active taxonomy version. Activate another version first.")
        await self.db.execute(
            text("DELETE FROM taxonomy_versions WHERE version=:version"),
            {"version": version},
        )
        await self.db.commit()
        return {"version": version, "deleted": True}
    async def activate(self,v:str):
        await self._ensure_activatable(v)
        await self.db.execute(text("UPDATE taxonomy_versions SET is_active=false WHERE is_active"))
        r=await self.db.execute(text("UPDATE taxonomy_versions SET is_active=true,published_at=now() WHERE version=:v"),{"v":v})
        if not r.rowcount:
            raise HTTPException(404,"Taxonomy version not found.")
        await self.db.commit()
        return {"version":v,"active":True}

    async def _ensure_activatable(self, version: str) -> None:
        if not await self.db.scalar(
            text("SELECT count(*) FROM taxonomy_concepts WHERE taxonomy_version=:v AND kind='skill' AND is_active"),
            {"v": version},
        ):
            raise HTTPException(422, "Taxonomy version needs an active skill.")
    async def upsert_concept(self,v:str,cid:str,p:TaxonomyConceptUpsert):
        if not await self.db.scalar(text("SELECT 1 FROM taxonomy_versions WHERE version=:v"),{"v":v}):
            raise HTTPException(404,"Taxonomy version not found.")
        aliases=list(dict.fromkeys([p.label,*(x.strip() for x in p.aliases if x.strip())]))
        await self.db.execute(text("INSERT INTO taxonomy_concepts(taxonomy_version,concept_id,label,kind,description,metadata,is_active) VALUES(:v,:id,:label,:kind,:description,CAST(:metadata AS jsonb),:active) ON CONFLICT(taxonomy_version,concept_id) DO UPDATE SET label=EXCLUDED.label,kind=EXCLUDED.kind,description=EXCLUDED.description,metadata=EXCLUDED.metadata,is_active=EXCLUDED.is_active"),{"v":v,"id":cid,"label":p.label,"kind":p.kind,"description":p.description,"metadata":json.dumps(p.metadata),"active":p.isActive})
        await self.db.execute(text("DELETE FROM taxonomy_aliases WHERE taxonomy_version=:v AND concept_id=:id"),{"v":v,"id":cid})
        for a in aliases:
            await self.db.execute(text("INSERT INTO taxonomy_aliases(taxonomy_version,concept_id,alias) VALUES(:v,:id,:a)"),{"v":v,"id":cid,"a":a})
        await self.db.commit()
        return {"version":v,"conceptId":cid,"aliases":aliases}
    async def delete_concept(self, version: str, concept_id: str) -> dict:
        exists = await self.db.scalar(
            text(
                "SELECT 1 FROM taxonomy_concepts "
                "WHERE taxonomy_version=:version AND concept_id=:concept_id"
            ),
            {"version": version, "concept_id": concept_id},
        )
        if not exists:
            raise HTTPException(404, "Taxonomy concept not found.")
        await self.db.execute(
            text(
                "DELETE FROM taxonomy_concepts "
                "WHERE taxonomy_version=:version AND concept_id=:concept_id"
            ),
            {"version": version, "concept_id": concept_id},
        )
        await self.db.commit()
        return {"version": version, "conceptId": concept_id, "deleted": True}
    async def upsert_relation(self,v:str,p:TaxonomyRelationUpsert):
        if p.sourceConceptId==p.targetConceptId:
            raise HTTPException(422,"A taxonomy relation cannot point to itself.")
        rows=await self.db.execute(text("SELECT concept_id FROM taxonomy_concepts WHERE taxonomy_version=:v AND concept_id IN (:s,:t)"),{"v":v,"s":p.sourceConceptId,"t":p.targetConceptId})
        if len(rows.scalars().all())!=2:
            raise HTTPException(422,"Both taxonomy concepts must exist in this version.")
        await self.db.execute(text("INSERT INTO taxonomy_relations(taxonomy_version,source_concept_id,target_concept_id,relation_type,weight,is_required,status) VALUES(:v,:s,:t,:r,:w,:required,:status) ON CONFLICT(taxonomy_version,source_concept_id,target_concept_id,relation_type) DO UPDATE SET weight=EXCLUDED.weight,is_required=EXCLUDED.is_required,status=EXCLUDED.status"),{"v":v,"s":p.sourceConceptId,"t":p.targetConceptId,"r":p.relationType,"w":p.weight,"required":p.isRequired,"status":p.status})
        await self.db.commit()
        return {"version":v,**p.model_dump()}
