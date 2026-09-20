"""HTTP controller only; rules and persistence live in TaxonomyAdminService."""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.security import require_admin
from src.infrastructure.database import get_db
from src.modules.taxonomy.admin_service import TaxonomyAdminService
from src.modules.taxonomy.schemas import TaxonomyConceptUpsert, TaxonomyRelationUpsert, TaxonomyVersionUpsert
router=APIRouter(prefix="/admin/taxonomy",tags=["taxonomy"])
@router.get("/template")
async def taxonomy_template(_:dict=Depends(require_admin))->Response:
    from openpyxl import Workbook
    import io
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Taxonomy"
    sheet.append(("concept_id", "label", "kind", "description", "is_active"))
    sheet.append(("example-skill", "Example Skill", "skill", "Replace this description", True))
    stream = io.BytesIO()
    workbook.save(stream)
    return Response(stream.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=taxonomy-template.xlsx"})
@router.get("/active")
async def active(_:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:return await TaxonomyAdminService(db).active()
@router.get("/versions")
async def versions(_:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:return {"items": await TaxonomyAdminService(db).versions()}
@router.post("/versions/{version}/clone")
async def clone_version(version:str,payload:dict,_:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:
    return {"version": version, "cloned": await TaxonomyAdminService(db).clone_version(str(payload.get("sourceVersion", "")), version)}
@router.get("/versions/{version}/export")
async def export_version(version:str,_:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->Response:
    return Response(await TaxonomyAdminService(db).export_xlsx(version), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename=taxonomy-{version}.xlsx"})
@router.post("/versions/{version}/import")
async def import_version(version:str,file:UploadFile=File(...),_:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:
    content = await file.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(422, "Taxonomy XLSX must not exceed 5 MB.")
    return {"version": version, "imported": await TaxonomyAdminService(db).import_xlsx(version, content)}
@router.put("/versions")
async def upsert_version(payload:TaxonomyVersionUpsert,_:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:return await TaxonomyAdminService(db).upsert_version(payload)
@router.post("/versions/{version}/activate")
async def activate(version:str,_:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:return await TaxonomyAdminService(db).activate(version)
@router.put("/versions/{version}/concepts/{concept_id}")
async def upsert_concept(version:str,concept_id:str,payload:TaxonomyConceptUpsert,_:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:return await TaxonomyAdminService(db).upsert_concept(version,concept_id,payload)
@router.put("/versions/{version}/relations")
async def upsert_relation(version:str,payload:TaxonomyRelationUpsert,_:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:return await TaxonomyAdminService(db).upsert_relation(version,payload)
