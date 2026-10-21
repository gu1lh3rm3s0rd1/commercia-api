"""Integration tests for the users listing endpoint — require a real Postgres with the
schema applied and scripts/seed_admin.py already run. Skipped automatically otherwise.
"""

from tests.conftest import requires_db

pytestmark = requires_db


def test_list_users_includes_seeded_admin(client, admin_headers):
    resp = client.get("/api/v1/users", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()

    admin = next(u for u in body["items"] if u["username"] == "admin")
    assert admin["is_superuser"] is True
    assert admin["is_active"] is True
    assert len(admin["accessible_filial_ids"]) >= 1
    assert "password_hash" not in admin


def test_list_users_requires_auth(client):
    resp = client.get("/api/v1/users")
    assert resp.status_code == 401
