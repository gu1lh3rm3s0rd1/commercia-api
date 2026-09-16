"""SQLAlchemy models — imported here so Base.metadata sees every table
(needed by Alembic's env.py, and by any future Base.metadata.create_all() use)."""

from app.models import catalog, company, sales, user  # noqa: F401
