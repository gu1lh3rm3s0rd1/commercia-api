"""Integration tests for the auth flow — require a real Postgres with the Fase 0 schema
applied and scripts/seed_admin.py already run (creates the 'admin' user). Skipped
automatically otherwise, see the `requires_db` marker in conftest.py."""

from tests.conftest import requires_db

pytestmark = requires_db


def test_login_and_me(client):
    response = client.post(
        "/api/v1/auth/login",
        data={"username": "admin", "password": "admin123"},
    )
    assert response.status_code == 200
    token = response.json()["access_token"]

    me_response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me_response.status_code == 200
    body = me_response.json()
    assert body["username"] == "admin"
    assert body["is_superuser"] is True
    assert len(body["accessible_filial_ids"]) >= 1


def test_login_wrong_password(client):
    response = client.post(
        "/api/v1/auth/login",
        data={"username": "admin", "password": "wrong"},
    )
    assert response.status_code == 401


def test_me_without_token(client):
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
