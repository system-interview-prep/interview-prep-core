"""HTTP controller only; rules and persistence live in TaxonomyAdminService."""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.admin_audit import record_admin_action
from src.core.roles import QUESTION_AUTHOR, QUESTION_BANK_ADMIN, QUESTION_REVIEWER
from src.core.security import require_admin, require_roles
from src.infrastructure.database import get_db
from src.modules.taxonomy.admin_service import TaxonomyAdminService
from src.modules.taxonomy.schemas import TaxonomyConceptUpsert, TaxonomyRelationUpsert, TaxonomyVersionUpsert

router=APIRouter(prefix="/admin/taxonomy",tags=["taxonomy"])
# Question authors map their questions to concepts and reviewers check that
# mapping, so the active taxonomy and its versions are readable to them too.
# Every change stays ADMIN-only.
_read_taxonomy = require_roles(QUESTION_AUTHOR, QUESTION_REVIEWER, QUESTION_BANK_ADMIN)
@router.get("/template")
async def taxonomy_template(_:dict=Depends(require_admin))->Response:
    import io

    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Taxonomy"
    sheet.append(("concept_id", "label", "kind", "description", "is_active"))
    sheet.append(("example-skill", "Example Skill", "skill", "Replace this description", True))
    stream = io.BytesIO()
    workbook.save(stream)
    return Response(stream.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=taxonomy-template.xlsx"})
@router.get("/active")
async def active(_:dict=Depends(_read_taxonomy),db:AsyncSession=Depends(get_db))->dict:return await TaxonomyAdminService(db).active()
@router.get("/versions")
async def versions(_:dict=Depends(_read_taxonomy),db:AsyncSession=Depends(get_db))->dict:return {"items": await TaxonomyAdminService(db).versions()}
@router.post("/versions/{version}/clone")
async def clone_version(version:str,payload:dict,actor:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:
    source = str(payload.get("sourceVersion", ""))
    cloned = await TaxonomyAdminService(db).clone_version(source, version)
    await record_admin_action(db, actor, "CLONE_TAXONOMY", target_id=version, detail={"source": source})
    return {"version": version, "cloned": cloned}
@router.get("/versions/{version}/export")
async def export_version(version:str,_:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->Response:
    return Response(await TaxonomyAdminService(db).export_xlsx(version), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename=taxonomy-{version}.xlsx"})
@router.post("/versions/{version}/import")
async def import_version(version:str,file:UploadFile=File(...),actor:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:
    content = await file.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(422, "Taxonomy XLSX must not exceed 5 MB.")
    imported = await TaxonomyAdminService(db).import_xlsx(version, content)
    await record_admin_action(db, actor, "IMPORT_TAXONOMY", target_id=version, detail={"imported": imported})
    return {"version": version, "imported": imported}
@router.put("/versions")
async def upsert_version(payload:TaxonomyVersionUpsert,actor:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:
    result = await TaxonomyAdminService(db).upsert_version(payload)
    detail = {"activate": payload.activate}
    await record_admin_action(db, actor, "EDIT_TAXONOMY_VERSION", target_id=payload.version, detail=detail)
    return result
@router.delete("/versions/{version}")
async def delete_version(version:str,actor:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:
    result = await TaxonomyAdminService(db).delete_version(version)
    await record_admin_action(db, actor, "DELETE_TAXONOMY_VERSION", target_id=version)
    return result
@router.post("/versions/{version}/activate")
async def activate(version:str,actor:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:
    result = await TaxonomyAdminService(db).activate(version)
    await record_admin_action(db, actor, "ACTIVATE_TAXONOMY", target_id=version)
    return result
@router.put("/versions/{version}/concepts/{concept_id}")
async def upsert_concept(version:str,concept_id:str,payload:TaxonomyConceptUpsert,actor:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:
    result = await TaxonomyAdminService(db).upsert_concept(version,concept_id,payload)
    detail = {"concept": concept_id}
    await record_admin_action(db, actor, "EDIT_TAXONOMY_CONCEPT", target_id=version, detail=detail)
    return result
@router.delete("/versions/{version}/concepts/{concept_id}")
async def delete_concept(version:str,concept_id:str,actor:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:
    result = await TaxonomyAdminService(db).delete_concept(version,concept_id)
    detail = {"concept": concept_id}
    await record_admin_action(db, actor, "DELETE_TAXONOMY_CONCEPT", target_id=version, detail=detail)
    return result
@router.put("/versions/{version}/relations")
async def upsert_relation(version:str,payload:TaxonomyRelationUpsert,actor:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:
    result = await TaxonomyAdminService(db).upsert_relation(version,payload)
    detail = {
        "relation": f"{payload.sourceConceptId} → {payload.targetConceptId}",
        "relationType": payload.relationType,
    }
    await record_admin_action(db, actor, "EDIT_TAXONOMY_RELATION", target_id=version, detail=detail)
    return result
