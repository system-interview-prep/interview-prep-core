"""HTTP controller only; rules and persistence live in TaxonomyAdminService."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.security import require_admin
from src.infrastructure.database import get_db
from src.modules.taxonomy.admin_service import TaxonomyAdminService
from src.modules.taxonomy.schemas import TaxonomyConceptUpsert, TaxonomyRelationUpsert, TaxonomyVersionUpsert
router=APIRouter(prefix="/admin/taxonomy",tags=["taxonomy"])
@router.get("/active")
async def active(_:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:return await TaxonomyAdminService(db).active()
@router.put("/versions")
async def upsert_version(payload:TaxonomyVersionUpsert,_:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:return await TaxonomyAdminService(db).upsert_version(payload)
@router.post("/versions/{version}/activate")
async def activate(version:str,_:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:return await TaxonomyAdminService(db).activate(version)
@router.put("/versions/{version}/concepts/{concept_id}")
async def upsert_concept(version:str,concept_id:str,payload:TaxonomyConceptUpsert,_:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:return await TaxonomyAdminService(db).upsert_concept(version,concept_id,payload)
@router.put("/versions/{version}/relations")
async def upsert_relation(version:str,payload:TaxonomyRelationUpsert,_:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:return await TaxonomyAdminService(db).upsert_relation(version,payload)
