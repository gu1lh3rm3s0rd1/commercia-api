from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

# create_engine does not connect eagerly — importing this module is safe even
# without a reachable database. The first real connection only happens when a
# session actually issues a query (e.g. via get_db in a request).
engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
