from fastapi import APIRouter

from app.modules.auth.router import router as auth_router
from app.modules.catalog.router import router as catalog_router

router = APIRouter()


router.include_router(auth_router, prefix="/auth", tags=["auth"])
router.include_router(catalog_router, prefix="/catalog", tags=["catalog"])
