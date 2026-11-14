from functools import lru_cache

from app.ai.providers.base import LLMProvider, LLMUnavailable
from app.core.config import get_settings


@lru_cache
def _gemini(api_key: str, models: tuple[str, ...]) -> LLMProvider:
    from app.ai.providers.gemini import GeminiProvider  # imported only when actually used

    return GeminiProvider(api_key, list(models))


def get_provider() -> LLMProvider:
    """FastAPI dependency (tests override it with a FakeProvider)."""
    settings = get_settings()
    if not settings.gemini_api_key:
        raise LLMUnavailable("not_configured", "GEMINI_API_KEY is not set")
    return _gemini(settings.gemini_api_key, tuple(settings.gemini_models))
