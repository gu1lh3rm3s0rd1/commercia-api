from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.ai.providers.base import LLMUnavailable
from app.api.v1.router import router as v1_router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(title=settings.app_name, version=settings.app_version)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(v1_router, prefix="/api/v1")

_UNAVAILABLE = {
    "not_configured": "O assistente não está configurado neste servidor.",
    "rate_limited": "O assistente atingiu o limite de uso no momento. Tente de novo em alguns minutos.",
    "unavailable": "O assistente está indisponível no momento. Tente de novo em instantes.",
}


@app.exception_handler(LLMUnavailable)
async def llm_unavailable(_request: Request, exc: LLMUnavailable) -> JSONResponse:
    """Chat (Fase 11): the model being unreachable is a 503 with a friendly message, never a 500."""
    return JSONResponse(status_code=503, content={"detail": _UNAVAILABLE.get(exc.reason, _UNAVAILABLE["unavailable"]),
                                                  "reason": exc.reason})


@app.get("/")
async def root():
    return {"message": "API is running"}


@app.get("/health")
async def health():
    return {"status": "ok"}
