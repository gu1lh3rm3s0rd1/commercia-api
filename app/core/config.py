from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Commercia API"
    app_version: str = "0.1.0"

    db_driver: str = "postgresql+psycopg"
    db_host: str
    db_port: int = 5432
    db_user: str
    db_password: str
    db_name: str
    db_sslmode: str = "prefer"  # Neon (e a maioria dos Postgres gerenciados) exige "require" em produção

    secret_key: str
    access_token_expire_minutes: int = 1440

    # Lista separada por vírgula — ex.: "http://localhost:5173,https://app.commercia.exemplo".
    # Necessário porque o app web (React) roda em outra origem que a API; o app mobile não
    # é afetado (CORS é restrição só de navegador).
    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    # Fuso do negócio: define o que é "hoje", onde o dia começa e termina nos relatórios
    # e qual preço está vigente. As datas continuam gravadas e devolvidas em UTC.
    app_timezone: str = "America/Sao_Paulo"

    # Chatbot (Fase 11, RF08). Sem chave a API sobe normalmente e só POST /chat responde 503.
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.5-flash-lite"
    # Tentados em ordem quando o modelo principal está sobrecarregado (503/504) — picos de
    # demanda no Google são comuns. Lista separada por vírgula; vazia = sem reserva.
    gemini_fallback_models: str = "gemini-3.6-flash,gemini-3.1-flash-lite"
    chat_rate_limit_per_minute: int = 20

    @property
    def database_url(self) -> URL:
        """Built dynamically from the individual credential fields above — never a
        hardcoded connection string. Returned as a SQLAlchemy URL object (not str) so the
        password is never manually escaped/serialized; create_engine() accepts it directly."""
        return URL.create(
            drivername=self.db_driver,
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
            query={"sslmode": self.db_sslmode},
        )

    @property
    def gemini_models(self) -> list[str]:
        extra = [m.strip() for m in self.gemini_fallback_models.split(",") if m.strip()]
        return [self.gemini_model, *[m for m in extra if m != self.gemini_model]]

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
