"""Integration tests for the Fase 5 mobile sync module (push/pull) — require a real
Postgres with the schema applied (including migration 0003) and scripts/seed_admin.py
already run. Skipped automatically otherwise.
"""

import random
import string
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text

from app.db.session import engine
from tests.conftest import requires_db

pytestmark = requires_db


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _unique_cpf() -> str:
    return "".join(random.choices(string.digits, k=11))


def _admin_filial_id(client, admin_headers) -> int:
    me = client.get("/api/v1/auth/me", headers=admin_headers).json()
    return me["accessible_filial_ids"][0]


def _movement_count(product_id: int) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM stock_movements WHERE product_id = :p"), {"p": product_id}
        ).scalar()


def _make_stocked_product(client, admin_headers, filial_id: int, price: str, qty: int) -> int:
    department_id = client.post(
        "/api/v1/catalog/departments", json={"name": _unique("Depto")}, headers=admin_headers
    ).json()["id"]
    category_id = client.post(
        f"/api/v1/catalog/categories?filial_id={filial_id}",
        json={"name": _unique("Categoria"), "department_id": department_id},
        headers=admin_headers,
    ).json()["id"]
    product_id = client.post(
        f"/api/v1/catalog/products?filial_id={filial_id}",
        json={"sku": _unique("SKU"), "name": "Produto sync", "category_id": category_id},
        headers=admin_headers,
    ).json()["id"]
    client.post(
        f"/api/v1/catalog/products/{product_id}/prices",
        json={"price_default": price},
        headers=admin_headers,
    )
    client.post(
        f"/api/v1/catalog/products/{product_id}/stock/adjustments",
        json={"quantity_delta": qty, "reason": "restock"},
        headers=admin_headers,
    )
    return product_id


def _make_client(client, admin_headers, filial_id: int) -> int:
    resp = client.post(
        f"/api/v1/clients?filial_id={filial_id}",
        json={
            "first_name": "Sync",
            "last_name": "Teste",
            "email": f"{_unique('c')}@example.com",
            "cpf": _unique_cpf(),
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------
# push
# ---------------------------------------------------------------------


def test_push_creates_sale_and_is_idempotent_on_resend(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "12.00", qty=10)
    client_id = _make_client(client, admin_headers, filial_id)
    movements_before = _movement_count(product_id)

    reference = str(uuid.uuid4())
    batch = {
        "sales": [
            {
                "client_reference": reference,
                "client_id": client_id,
                "payment_method": "cash",
                "occurred_at": _now_iso(),
                "items": [{"product_id": product_id, "quantity": 2}],
            }
        ]
    }

    first = client.post(f"/api/v1/sync/push?filial_id={filial_id}", json=batch, headers=admin_headers)
    assert first.status_code == 200
    first_result = first.json()["results"][0]
    assert first_result["status"] == "created"
    order_id = first_result["order_id"]

    stock_after_first = client.get(
        f"/api/v1/catalog/products/{product_id}/stock", headers=admin_headers
    ).json()
    assert stock_after_first["quantity"] == 8  # 10 - 2

    # resend the SAME batch (simulating a mobile retry after a dropped connection)
    second = client.post(f"/api/v1/sync/push?filial_id={filial_id}", json=batch, headers=admin_headers)
    assert second.status_code == 200
    second_result = second.json()["results"][0]
    assert second_result["status"] == "duplicate"
    assert second_result["order_id"] == order_id

    stock_after_resend = client.get(
        f"/api/v1/catalog/products/{product_id}/stock", headers=admin_headers
    ).json()
    assert stock_after_resend["quantity"] == 8  # unchanged — not decremented twice

    assert _movement_count(product_id) == movements_before + 1  # only one movement, not two

    # the order should carry the occurred_at we sent, and the client_reference
    order = client.get(f"/api/v1/orders/{order_id}", headers=admin_headers).json()
    assert order["client_reference"] == reference
    assert order["occurred_at"] is not None


def test_push_rejects_whole_sale_on_insufficient_stock(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "5.00", qty=1)
    client_id = _make_client(client, admin_headers, filial_id)

    batch = {
        "sales": [
            {
                "client_reference": str(uuid.uuid4()),
                "client_id": client_id,
                "payment_method": "cash",
                "occurred_at": _now_iso(),
                "items": [{"product_id": product_id, "quantity": 5}],  # more than the 1 available
            }
        ]
    }

    resp = client.post(f"/api/v1/sync/push?filial_id={filial_id}", json=batch, headers=admin_headers)
    assert resp.status_code == 200
    result = resp.json()["results"][0]
    assert result["status"] == "rejected"
    assert result["order_id"] is None

    stock = client.get(f"/api/v1/catalog/products/{product_id}/stock", headers=admin_headers).json()
    assert stock["quantity"] == 1  # untouched


def test_push_batch_processes_each_sale_independently(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    good_product = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=10)
    bad_product = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=0)
    client_id = _make_client(client, admin_headers, filial_id)

    batch = {
        "sales": [
            {
                "client_reference": str(uuid.uuid4()),
                "client_id": client_id,
                "payment_method": "cash",
                "occurred_at": _now_iso(),
                "items": [{"product_id": good_product, "quantity": 1}],
            },
            {
                "client_reference": str(uuid.uuid4()),
                "client_id": client_id,
                "payment_method": "cash",
                "occurred_at": _now_iso(),
                "items": [{"product_id": bad_product, "quantity": 1}],
            },
        ]
    }

    resp = client.post(f"/api/v1/sync/push?filial_id={filial_id}", json=batch, headers=admin_headers)
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[0]["status"] == "created"
    assert results[1]["status"] == "rejected"


# ---------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------


def test_pull_without_last_synced_at_returns_full_catalog(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "7.00", qty=5)

    resp = client.get(f"/api/v1/sync/pull?filial_id={filial_id}", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert any(p["id"] == product_id for p in body["products"])
    assert "synced_at" in body


def test_pull_incremental_only_returns_the_delta(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "7.00", qty=5)

    checkpoint = client.get(f"/api/v1/sync/pull?filial_id={filial_id}", headers=admin_headers).json()[
        "synced_at"
    ]

    # nothing changed yet — pulling again from this checkpoint should show no products
    empty_pull = client.get(
        f"/api/v1/sync/pull?filial_id={filial_id}&last_synced_at={checkpoint}", headers=admin_headers
    ).json()
    assert not any(p["id"] == product_id for p in empty_pull["products"])

    # edit the product — this is exactly what the missing updated_at column would have hidden
    client.patch(
        f"/api/v1/catalog/products/{product_id}",
        json={"name": "Produto sync (editado)"},
        headers=admin_headers,
    )

    delta_pull = client.get(
        f"/api/v1/sync/pull?filial_id={filial_id}&last_synced_at={checkpoint}", headers=admin_headers
    ).json()
    assert any(p["id"] == product_id for p in delta_pull["products"])


def test_write_endpoints_require_auth(client):
    resp = client.post("/api/v1/sync/push?filial_id=1", json={"sales": []})
    assert resp.status_code == 401
