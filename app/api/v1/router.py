from fastapi import APIRouter

from app.modules.analytics.router import router as analytics_router
from app.modules.audit.router import router as audit_router
from app.modules.auth.router import router as auth_router
from app.modules.catalog.router import router as catalog_router
from app.modules.clients.router import router as clients_router
from app.modules.companies.router import router as companies_router
from app.modules.dashboard.router import router as dashboard_router
from app.modules.forecasts.router import router as forecasts_router
from app.modules.sales.router import router as sales_router
from app.modules.sync.router import router as sync_router
from app.modules.users.router import router as users_router

router = APIRouter()


router.include_router(auth_router, prefix="/auth", tags=["auth"])
router.include_router(catalog_router, prefix="/catalog", tags=["catalog"])
router.include_router(clients_router, prefix="/clients", tags=["clients"])
router.include_router(sales_router, prefix="/orders", tags=["orders"])
router.include_router(sync_router, prefix="/sync", tags=["sync"])
router.include_router(dashboard_router, prefix="/dashboard", tags=["dashboard"])
router.include_router(companies_router, prefix="/companies", tags=["companies"])
router.include_router(users_router, prefix="/users", tags=["users"])
router.include_router(forecasts_router, prefix="/forecasts", tags=["forecasts"])
router.include_router(analytics_router, prefix="/analytics", tags=["analytics"])
router.include_router(audit_router, prefix="/audit-logs", tags=["audit"])
