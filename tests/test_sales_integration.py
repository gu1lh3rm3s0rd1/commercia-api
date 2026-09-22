"""Integration tests for the Fase 4 sales module — require a real Postgres with the
schema applied and scripts/seed_admin.py already run. Skipped automatically otherwise.
"""

import random
import string
import uuid
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
        json={"sku": _unique("SKU"), "name": "Produto de venda", "category_id": category_id},
        headers=admin_headers,
    ).json()["id"]
    price_resp = client.post(
        f"/api/v1/catalog/products/{product_id}/prices",
        json={"price_default": price},
        headers=admin_headers,
    )
    assert price_resp.status_code == 201
    adjust_resp = client.post(
        f"/api/v1/catalog/products/{product_id}/stock/adjustments",
        json={"quantity_delta": qty, "reason": "restock"},
        headers=admin_headers,
    )
    assert adjust_resp.status_code == 201
    return product_id


def _make_client(client, admin_headers, filial_id: int) -> int:
    resp = client.post(
        f"/api/v1/clients?filial_id={filial_id}",
        json={"first_name": "Comprador", "last_name": "Teste", "email": f"{_unique('c')}@example.com", "cpf": _unique_cpf()},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def test_order_create_computes_total_and_writes_stock_movements(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=20)
    client_id = _make_client(client, admin_headers, filial_id)
    movements_before = _movement_count(product_id)

    order_resp = client.post(
        f"/api/v1/orders?filial_id={filial_id}",
        json={
            "client_id": client_id,
            "payment_method": "cash",
            "discount": "5.00",
            "items": [{"product_id": product_id, "quantity": 3}],
        },
        headers=admin_headers,
    )
    assert order_resp.status_code == 201
    order = order_resp.json()
    assert Decimal(str(order["total"])) == Decimal("25.00")  # 3*10.00 - 5.00
    assert order["status"] == "pending"
    assert len(order["items"]) == 1
    assert order["items"][0]["unit_price"] == "10.00" or Decimal(str(order["items"][0]["unit_price"])) == Decimal("10.00")

    stock_resp = client.get(f"/api/v1/catalog/products/{product_id}/stock", headers=admin_headers)
    assert stock_resp.json()["quantity"] == 17  # 20 - 3

    assert _movement_count(product_id) == movements_before + 1  # exactly one movement per item


def test_order_insufficient_stock_rejected(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "5.00", qty=1)
    client_id = _make_client(client, admin_headers, filial_id)

    resp = client.post(
        f"/api/v1/orders?filial_id={filial_id}",
        json={
            "client_id": client_id,
            "payment_method": "cash",
            "items": [{"product_id": product_id, "quantity": 5}],
        },
        headers=admin_headers,
    )
    assert resp.status_code == 400

    stock_resp = client.get(f"/api/v1/catalog/products/{product_id}/stock", headers=admin_headers)
    assert stock_resp.json()["quantity"] == 1  # untouched — rejected before any write


def test_order_discount_exceeding_subtotal_rejected(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=10)
    client_id = _make_client(client, admin_headers, filial_id)

    resp = client.post(
        f"/api/v1/orders?filial_id={filial_id}",
        json={
            "client_id": client_id,
            "payment_method": "cash",
            "discount": "999.00",
            "items": [{"product_id": product_id, "quantity": 1}],
        },
        headers=admin_headers,
    )
    assert resp.status_code == 400


def test_cancel_reverses_stock_with_new_movement(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "8.00", qty=10)
    client_id = _make_client(client, admin_headers, filial_id)

    order_resp = client.post(
        f"/api/v1/orders?filial_id={filial_id}",
        json={
            "client_id": client_id,
            "payment_method": "pix",
            "items": [{"product_id": product_id, "quantity": 4}],
        },
        headers=admin_headers,
    )
    order_id = order_resp.json()["id"]
    movements_after_sale = _movement_count(product_id)

    stock_after_sale = client.get(f"/api/v1/catalog/products/{product_id}/stock", headers=admin_headers).json()
    assert stock_after_sale["quantity"] == 6  # 10 - 4

    cancel_resp = client.post(f"/api/v1/orders/{order_id}/cancel", headers=admin_headers)
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelled"

    stock_after_cancel = client.get(f"/api/v1/catalog/products/{product_id}/stock", headers=admin_headers).json()
    assert stock_after_cancel["quantity"] == 10  # reversed back

    assert _movement_count(product_id) == movements_after_sale + 1  # reversal is a NEW movement

    # cancelling twice is rejected, not silently re-reversed
    second_cancel = client.post(f"/api/v1/orders/{order_id}/cancel", headers=admin_headers)
    assert second_cancel.status_code == 409


def test_pay_then_get_order(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "3.50", qty=5)
    client_id = _make_client(client, admin_headers, filial_id)

    order_resp = client.post(
        f"/api/v1/orders?filial_id={filial_id}",
        json={
            "client_id": client_id,
            "payment_method": "debit_card",
            "items": [{"product_id": product_id, "quantity": 2}],
        },
        headers=admin_headers,
    )
    order_id = order_resp.json()["id"]

    pay_resp = client.post(f"/api/v1/orders/{order_id}/pay", headers=admin_headers)
    assert pay_resp.status_code == 200
    assert pay_resp.json()["status"] == "paid"

    get_resp = client.get(f"/api/v1/orders/{order_id}", headers=admin_headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "paid"

    # can't pay an already-paid order again
    second_pay = client.post(f"/api/v1/orders/{order_id}/pay", headers=admin_headers)
    assert second_pay.status_code == 400


def test_write_endpoints_require_auth(client):
    resp = client.post("/api/v1/orders?filial_id=1", json={})
    assert resp.status_code == 401


def test_negative_discount_rejected_as_422(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=5)
    client_id = _make_client(client, admin_headers, filial_id)

    resp = client.post(
        f"/api/v1/orders?filial_id={filial_id}",
        json={
            "client_id": client_id,
            "payment_method": "cash",
            "discount": "-1.00",
            "items": [{"product_id": product_id, "quantity": 1}],
        },
        headers=admin_headers,
    )
    assert resp.status_code == 422


def test_zero_or_negative_quantity_rejected_as_422(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=5)
    client_id = _make_client(client, admin_headers, filial_id)

    for bad_qty in (0, -3):
        resp = client.post(
            f"/api/v1/orders?filial_id={filial_id}",
            json={
                "client_id": client_id,
                "payment_method": "cash",
                "items": [{"product_id": product_id, "quantity": bad_qty}],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 422


def test_invalid_payment_method_rejected_as_422(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=5)
    client_id = _make_client(client, admin_headers, filial_id)

    resp = client.post(
        f"/api/v1/orders?filial_id={filial_id}",
        json={
            "client_id": client_id,
            "payment_method": "bitcoin",
            "items": [{"product_id": product_id, "quantity": 1}],
        },
        headers=admin_headers,
    )
    assert resp.status_code == 422


def test_order_referencing_nonexistent_product_rejected(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    client_id = _make_client(client, admin_headers, filial_id)

    resp = client.post(
        f"/api/v1/orders?filial_id={filial_id}",
        json={
            "client_id": client_id,
            "payment_method": "cash",
            "items": [{"product_id": 999999999, "quantity": 1}],
        },
        headers=admin_headers,
    )
    assert resp.status_code == 400


def test_get_nonexistent_order_is_404(client, admin_headers):
    resp = client.get("/api/v1/orders/999999999", headers=admin_headers)
    assert resp.status_code == 404
