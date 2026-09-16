import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.session import engine
from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _schema_applied() -> bool:
    """True once the Fase 0 migration has been applied by the user to the configured
    database (this project never runs migrations itself — see commercia-never-run-migrations).
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM users LIMIT 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not _schema_applied(),
    reason="Postgres schema not applied yet — apply the Fase 0 migration and run "
    "scripts/seed_admin.py yourself first (this project never runs migrations automatically).",
)


@pytest.fixture
def admin_headers(client: TestClient) -> dict[str, str]:
    """Bearer token for the seeded admin user — only usable in tests already marked
    with `requires_db` (scripts/seed_admin.py must have been run)."""
    response = client.post(
        "/api/v1/auth/login", data={"username": "admin", "password": "admin123"}
    )
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
