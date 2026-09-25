from fastapi import APIRouter

from app.modules.auth.router import router as auth_router
from app.modules.catalog.router import router as catalog_router
from app.modules.clients.router import router as clients_router
from app.modules.sales.router import router as sales_router
from app.modules.sync.router import router as sync_router

router = APIRouter()


router.include_router(auth_router, prefix="/auth", tags=["auth"])
router.include_router(catalog_router, prefix="/catalog", tags=["catalog"])
router.include_router(clients_router, prefix="/clients", tags=["clients"])
router.include_router(sales_router, prefix="/orders", tags=["orders"])
router.include_router(sync_router, prefix="/sync", tags=["sync"])
