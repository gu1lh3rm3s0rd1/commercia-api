"""Integration tests for the companies (filiais) reference endpoint — require a real
Postgres with the schema applied and scripts/seed_admin.py already run. Skipped
automatically otherwise.
"""

from tests.conftest import requires_db

pytestmark = requires_db


def test_list_companies_includes_accessible_filial(client, admin_headers):
    me = client.get("/api/v1/auth/me", headers=admin_headers).json()
    filial_id = me["accessible_filial_ids"][0]

    resp = client.get("/api/v1/companies", headers=admin_headers)
    assert resp.status_code == 200
    items = resp.json()

    match = next(c for c in items if c["id"] == filial_id)
    assert match["filial_code"]
    assert match["corporate_name"]
    assert match["cnpj"]
    assert "created_at" in match


def test_list_companies_requires_auth(client):
    resp = client.get("/api/v1/companies")
    assert resp.status_code == 401
