from fastapi import FastAPI

from app.api.v1.router import router as v1_router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(title=settings.app_name, version=settings.app_version)

app.include_router(v1_router, prefix="/api/v1")


@app.get("/")
async def root():
    return {"message": "API is running"}


@app.get("/health")
async def health():
    return {"status": "ok"}
