from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.db.base import Base
from app.models import catalog, company, sales, user  # noqa: F401 — registers tables on Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Database URL comes from app settings (.env), never hardcoded in alembic.ini.
# set_main_option needs a plain string; create_engine (used elsewhere, e.g. app/db/session.py)
# gets the URL object directly instead. IMPORTANT: str(url) masks the password as "***"
# (SQLAlchemy's default repr safeguard) — render_as_string(hide_password=False) is required
# here, otherwise Alembic would try to authenticate with the literal string "***".
config.set_main_option(
    "sqlalchemy.url", get_settings().database_url.render_as_string(hide_password=False)
)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
