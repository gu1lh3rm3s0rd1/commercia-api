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

    secret_key: str
    access_token_expire_minutes: int = 1440

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
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
