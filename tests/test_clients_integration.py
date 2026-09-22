"""Integration tests for the Fase 3 clients module — require a real Postgres with the
schema applied and scripts/seed_admin.py already run. Skipped automatically otherwise.

cpf/email uniqueness is GLOBAL (not per-filial, confirmed 2026-09-17), so tests use
random values to stay re-runnable against the real dev DB.
"""

import random
import string
import uuid

from tests.conftest import requires_db

pytestmark = requires_db


def _unique_cpf() -> str:
    return "".join(random.choices(string.digits, k=11))


def _unique_email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.com"


def _admin_filial_id(client, admin_headers) -> int:
    me = client.get("/api/v1/auth/me", headers=admin_headers).json()
    return me["accessible_filial_ids"][0]


def test_client_full_flow(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)

    create_resp = client.post(
        f"/api/v1/clients?filial_id={filial_id}",
        json={
            "first_name": "Maria",
            "last_name": "Silva",
            "email": _unique_email(),
            "cpf": _unique_cpf(),
        },
        headers=admin_headers,
    )
    assert create_resp.status_code == 201
    data = create_resp.json()
    assert data["status"] == "active"
    client_id = data["id"]

    get_resp = client.get(f"/api/v1/clients/{client_id}", headers=admin_headers)
    assert get_resp.status_code == 200

    list_resp = client.get(f"/api/v1/clients?filial_id={filial_id}", headers=admin_headers)
    assert list_resp.status_code == 200
    assert any(c["id"] == client_id for c in list_resp.json()["items"])

    update_resp = client.patch(
        f"/api/v1/clients/{client_id}", json={"phone": "11999999999"}, headers=admin_headers
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["phone"] == "11999999999"

    address_resp = client.post(
        f"/api/v1/clients/{client_id}/addresses",
        json={"street": "Rua Teste", "city": "São Paulo", "state": "SP", "zip_code": "01234567"},
        headers=admin_headers,
    )
    assert address_resp.status_code == 201
    address_id = address_resp.json()["id"]

    list_addr_resp = client.get(f"/api/v1/clients/{client_id}/addresses", headers=admin_headers)
    assert list_addr_resp.status_code == 200
    assert len(list_addr_resp.json()) == 1

    update_addr_resp = client.patch(
        f"/api/v1/clients/{client_id}/addresses/{address_id}",
        json={"number": "100"},
        headers=admin_headers,
    )
    assert update_addr_resp.status_code == 200
    assert update_addr_resp.json()["number"] == "100"

    delete_addr_resp = client.delete(
        f"/api/v1/clients/{client_id}/addresses/{address_id}", headers=admin_headers
    )
    assert delete_addr_resp.status_code == 204

    deactivate_resp = client.delete(f"/api/v1/clients/{client_id}", headers=admin_headers)
    assert deactivate_resp.status_code == 200
    assert deactivate_resp.json()["status"] == "inactive"


def test_duplicate_cpf_rejected(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    cpf = _unique_cpf()

    first = client.post(
        f"/api/v1/clients?filial_id={filial_id}",
        json={"first_name": "A", "last_name": "B", "email": _unique_email(), "cpf": cpf},
        headers=admin_headers,
    )
    assert first.status_code == 201

    duplicate = client.post(
        f"/api/v1/clients?filial_id={filial_id}",
        json={"first_name": "C", "last_name": "D", "email": _unique_email(), "cpf": cpf},
        headers=admin_headers,
    )
    assert duplicate.status_code == 409


def test_duplicate_email_rejected(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    email = _unique_email()

    first = client.post(
        f"/api/v1/clients?filial_id={filial_id}",
        json={"first_name": "A", "last_name": "B", "email": email, "cpf": _unique_cpf()},
        headers=admin_headers,
    )
    assert first.status_code == 201

    duplicate = client.post(
        f"/api/v1/clients?filial_id={filial_id}",
        json={"first_name": "C", "last_name": "D", "email": email, "cpf": _unique_cpf()},
        headers=admin_headers,
    )
    assert duplicate.status_code == 409


def test_invalid_cpf_format_rejected(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    resp = client.post(
        f"/api/v1/clients?filial_id={filial_id}",
        json={"first_name": "A", "last_name": "B", "email": _unique_email(), "cpf": "123"},
        headers=admin_headers,
    )
    assert resp.status_code == 422


def test_write_endpoints_require_auth(client):
    resp = client.post("/api/v1/clients?filial_id=1", json={})
    assert resp.status_code == 401
