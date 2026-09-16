"""Integration tests for the Fase 2 catalog module — require a real Postgres with the
Fase 0 schema applied and scripts/seed_admin.py already run. Skipped automatically
otherwise, see the `requires_db` marker in conftest.py.

Note: these run against the real configured database (no test-DB isolation/rollback in
this project yet), so every test uses a random suffix on names/SKUs to stay re-runnable.
"""

import uuid
from decimal import Decimal

from tests.conftest import requires_db

pytestmark = requires_db


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _admin_filial_id(client, admin_headers) -> int:
    me = client.get("/api/v1/auth/me", headers=admin_headers).json()
    return me["accessible_filial_ids"][0]


def _create_department(client, admin_headers) -> int:
    resp = client.post(
        "/api/v1/catalog/departments", json={"name": _unique("Depto")}, headers=admin_headers
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _create_category(client, admin_headers, filial_id: int, department_id: int) -> int:
    resp = client.post(
        f"/api/v1/catalog/categories?filial_id={filial_id}",
        json={"name": _unique("Categoria"), "department_id": department_id},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def test_department_create_and_list(client, admin_headers):
    department_id = _create_department(client, admin_headers)

    list_resp = client.get("/api/v1/catalog/departments", headers=admin_headers)
    assert list_resp.status_code == 200
    assert any(d["id"] == department_id for d in list_resp.json())


def test_category_duplicate_name_rejected(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    department_id = _create_department(client, admin_headers)
    name = _unique("Categoria")

    first = client.post(
        f"/api/v1/catalog/categories?filial_id={filial_id}",
        json={"name": name, "department_id": department_id},
        headers=admin_headers,
    )
    assert first.status_code == 201

    duplicate = client.post(
        f"/api/v1/catalog/categories?filial_id={filial_id}",
        json={"name": name, "department_id": department_id},
        headers=admin_headers,
    )
    assert duplicate.status_code == 409


def test_category_requires_filial_id(client, admin_headers):
    department_id = _create_department(client, admin_headers)
    resp = client.post(
        "/api/v1/catalog/categories",
        json={"name": _unique("Categoria"), "department_id": department_id},
        headers=admin_headers,
    )
    assert resp.status_code == 422  # filial_id is a required query param for write endpoints


def test_product_full_flow(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    department_id = _create_department(client, admin_headers)
    category_id = _create_category(client, admin_headers, filial_id, department_id)

    product_resp = client.post(
        f"/api/v1/catalog/products?filial_id={filial_id}",
        json={"sku": _unique("SKU"), "name": "Produto de teste", "category_id": category_id},
        headers=admin_headers,
    )
    assert product_resp.status_code == 201
    product = product_resp.json()
    assert product["status"] == "active"
    product_id = product["id"]

    price_resp = client.post(
        f"/api/v1/catalog/products/{product_id}/prices",
        json={"price_default": "19.90"},
        headers=admin_headers,
    )
    assert price_resp.status_code == 201

    current_price = client.get(
        f"/api/v1/catalog/products/{product_id}/prices/current", headers=admin_headers
    )
    assert current_price.status_code == 200
    assert Decimal(str(current_price.json()["price_default"])) == Decimal("19.90")

    stock_resp = client.get(f"/api/v1/catalog/products/{product_id}/stock", headers=admin_headers)
    assert stock_resp.status_code == 200
    assert stock_resp.json()["quantity"] == 0

    adjust_resp = client.post(
        f"/api/v1/catalog/products/{product_id}/stock/adjustments",
        json={"quantity_delta": 10, "reason": "restock"},
        headers=admin_headers,
    )
    assert adjust_resp.status_code == 201
    assert adjust_resp.json()["quantity"] == 10

    negative_adjust = client.post(
        f"/api/v1/catalog/products/{product_id}/stock/adjustments",
        json={"quantity_delta": -100, "reason": "loss"},
        headers=admin_headers,
    )
    assert negative_adjust.status_code == 400

    cost_resp = client.put(
        f"/api/v1/catalog/products/{product_id}/cost",
        json={"cost_value": "9.50"},
        headers=admin_headers,
    )
    assert cost_resp.status_code == 200
    assert Decimal(str(cost_resp.json()["cost_value"])) == Decimal("9.50")

    delete_resp = client.delete(f"/api/v1/catalog/products/{product_id}", headers=admin_headers)
    assert delete_resp.status_code == 200
    assert delete_resp.json()["status"] == "inactive"


def test_write_endpoints_require_auth(client):
    resp = client.post("/api/v1/catalog/departments", json={"name": "x"})
    assert resp.status_code == 401
