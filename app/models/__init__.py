"""SQLAlchemy models — imported here so Base.metadata sees every table
(needed by Alembic's env.py, and by any future Base.metadata.create_all() use)."""

from app.models import audit, catalog, chat, company, purchases, sales, user  # noqa: F401
