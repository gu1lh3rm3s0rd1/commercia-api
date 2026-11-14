"""Etapa 3: purchases (panel) and offline purchases (sync push). Require a real Postgres with
migrations up to 0015 applied; skipped otherwise."""

import random
import string
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.db.session import engine
from tests.conftest import requires_db


def _purchases_applied() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM purchases LIMIT 1"))
        return True
    except Exception:
        return False


pytestmark = [requires_db, pytest.mark.skipif(not _purchases_applied(), reason="migration 0015 not applied")]

PAST = "2020-01-01"


def _u() -> str:
    return uuid.uuid4().hex[:8]


def _digits(n: int) -> str:
    return "".join(random.choices(string.digits, k=n))


def _filial(client, h) -> int:
    return client.get("/api/v1/auth/me", headers=h).json()["accessible_filial_ids"][0]


def _product(client, h, f: int, stock: int = 0) -> int:
    dep = client.post("/api/v1/catalog/departments", json={"name": f"D{_u()}"}, headers=h).json()["id"]
    cat = client.post(f"/api/v1/catalog/categories?filial_id={f}", json={"name": f"C{_u()}", "department_id": dep},
                      headers=h).json()["id"]
    pid = client.post(f"/api/v1/catalog/products?filial_id={f}", json={"sku": f"S{_u()}", "name": "Prod compra",
                      "category_id": cat}, headers=h).json()["id"]
    client.post(f"/api/v1/catalog/products/{pid}/prices", json={"price_default": "20.00", "start_date": PAST}, headers=h)
    if stock:
        client.post(f"/api/v1/catalog/products/{pid}/stock/adjustments", json={"quantity_delta": stock, "reason": "restock"},
                    headers=h)
    return pid


def _supplier(client, h, f: int, roles=("supplier",)) -> int:
    resp = client.post(f"/api/v1/people?filial_id={f}", json={
        "person_type": "company", "roles": list(roles), "corporate_name": f"Fornecedor {_u()} LTDA", "cnpj": _digits(14)},
        headers=h)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _stock(client, h, pid: int) -> int:
    return client.get(f"/api/v1/catalog/products/{pid}/stock", headers=h).json()["quantity"]


def _cost(client, h, pid: int) -> Decimal:
    return Decimal(client.get(f"/api/v1/catalog/products/{pid}", headers=h).json()["cost"]["cost_value"])


def _buy(client, h, f: int, supplier: int, items: list[dict], **extra):
    return client.post(f"/api/v1/purchases?filial_id={f}", json={"supplier_id": supplier, "items": items, **extra}, headers=h)


def _user(client, admin_h, f: int, profile: str) -> dict[str, str]:
    groups = {g["name"]: g["id"] for g in client.get("/api/v1/users/groups", headers=admin_h).json()}
    username = f"u{_u()}"
    client.post("/api/v1/users", json={"full_name": "Pessoa Compra", "username": username, "email": f"{username}@example.com",
                                        "password": "senha123", "group_ids": [groups[profile]], "filial_ids": [f]},
                headers=admin_h)
    token = client.post("/api/v1/auth/login", data={"username": username, "password": "senha123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------
# panel flow
# ---------------------------------------------------------------------


def test_pending_then_received_moves_stock_and_cost_only_on_receipt(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    a, b, sup = _product(client, h, f, stock=2), _product(client, h, f), _supplier(client, h, f)
    resp = _buy(client, h, f, sup, [{"product_id": a, "quantity": 10, "unit_cost": "3.00"},
                                    {"product_id": b, "quantity": 5, "unit_cost": "4.00"}],
                discount="5.00", freight="10.00", invoice_number="12345")
    assert resp.status_code == 201, resp.text
    p = resp.json()
    assert p["status"] == "pending" and p["received_at"] is None
    assert Decimal(p["total"]) == Decimal("55.00")  # 30 + 20 - 5 + 10
    assert sum(Decimal(i["discount_allocated"]) for i in p["items"]) == Decimal("5.00")
    assert sum(Decimal(i["freight_allocated"]) for i in p["items"]) == Decimal("10.00")
    assert sum(Decimal(i["net_total"]) for i in p["items"]) == Decimal("55.00")
    assert _stock(client, h, a) == 2  # nothing in yet

    received = client.post(f"/api/v1/purchases/{p['id']}/receive", headers=h).json()
    assert received["status"] == "received" and received["received_at"]
    assert _stock(client, h, a) == 12 and _stock(client, h, b) == 5
    # a: 30 of 50 → discount 3.00, freight 6.00 → (30 - 3 + 6) / 10 = 3.30
    assert _cost(client, h, a) == Decimal("3.30")
    assert _cost(client, h, b) == Decimal("4.40")  # (20 - 2 + 4) / 5
    assert client.post(f"/api/v1/purchases/{p['id']}/receive", headers=h).status_code == 400  # only once

    with engine.connect() as conn:
        reasons = conn.execute(text("""SELECT m.reason::text, m.change_qty FROM stock_movements m
                                       JOIN purchase_items i ON i.id = m.purchase_item_id WHERE i.purchase_id = :p
                                       ORDER BY m.id"""), {"p": p["id"]}).all()
    assert reasons == [("purchase", 10), ("purchase", 5)]


def test_create_already_received(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    a, sup = _product(client, h, f), _supplier(client, h, f)
    p = _buy(client, h, f, sup, [{"product_id": a, "quantity": 4, "unit_cost": "2.50"}], receive=True).json()
    assert p["status"] == "received" and _stock(client, h, a) == 4 and _cost(client, h, a) == Decimal("2.50")


def test_cancel_pending_and_received(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    a, sup = _product(client, h, f), _supplier(client, h, f)
    pending = _buy(client, h, f, sup, [{"product_id": a, "quantity": 3, "unit_cost": "1.00"}]).json()
    assert client.post(f"/api/v1/purchases/{pending['id']}/cancel", headers=h).json()["status"] == "cancelled"
    assert client.post(f"/api/v1/purchases/{pending['id']}/cancel", headers=h).status_code == 409
    assert client.post(f"/api/v1/purchases/{pending['id']}/receive", headers=h).status_code == 400

    received = _buy(client, h, f, sup, [{"product_id": a, "quantity": 3, "unit_cost": "1.00"}], receive=True).json()
    assert _stock(client, h, a) == 3
    assert client.post(f"/api/v1/purchases/{received['id']}/cancel", headers=h).json()["status"] == "cancelled"
    assert _stock(client, h, a) == 0
    assert _cost(client, h, a) == Decimal("1.00")  # the cost stays


def test_cannot_cancel_a_purchase_whose_goods_were_sold(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    a, sup = _product(client, h, f), _supplier(client, h, f)
    p = _buy(client, h, f, sup, [{"product_id": a, "quantity": 2, "unit_cost": "5.00"}], receive=True).json()
    buyer = client.post(f"/api/v1/clients?filial_id={f}", json={"first_name": "C", "last_name": "D",
                        "email": f"{_u()}@example.com", "cpf": _digits(11)}, headers=h).json()["id"]
    assert client.post(f"/api/v1/orders?filial_id={f}", json={"client_id": buyer, "payment_method": "cash",
                       "items": [{"product_id": a, "quantity": 1}]}, headers=h).status_code == 201
    resp = client.post(f"/api/v1/purchases/{p['id']}/cancel", headers=h)
    assert resp.status_code == 409 and "already sold" in resp.json()["detail"]
    assert _stock(client, h, a) == 1  # untouched


def test_validation(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    a, sup = _product(client, h, f), _supplier(client, h, f)
    only_client = client.post(f"/api/v1/clients?filial_id={f}", json={"first_name": "C", "last_name": "D",
                              "email": f"{_u()}@example.com", "cpf": _digits(11)}, headers=h).json()["id"]
    line = [{"product_id": a, "quantity": 1, "unit_cost": "1.00"}]
    assert _buy(client, h, f, only_client, line).status_code == 400  # not a supplier
    assert _buy(client, h, f, sup, [{"product_id": 999999, "quantity": 1, "unit_cost": "1.00"}]).status_code == 400
    assert _buy(client, h, f, sup, line, discount="2.00").status_code == 400  # more than the subtotal
    assert _buy(client, h, f, sup, [{"product_id": a, "quantity": 0, "unit_cost": "1.00"}]).status_code == 422
    assert _buy(client, h, f, sup, [{"product_id": a, "quantity": 1, "unit_cost": "1.001"}]).status_code == 422
    assert _buy(client, h, f, sup, line, invoice_key="123").status_code == 422
    assert _buy(client, h, f, sup, line, unit_price="1.00").status_code == 422  # unknown field
    client.delete(f"/api/v1/people/{sup}", headers=h)
    assert "inactive" in _buy(client, h, f, sup, line).json()["detail"]


def test_list_filters_and_permissions(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    a, sup = _product(client, h, f), _supplier(client, h, f)
    mine = _buy(client, h, f, sup, [{"product_id": a, "quantity": 1, "unit_cost": "1.00"}]).json()["id"]
    listed = client.get(f"/api/v1/purchases?supplier_id={sup}&status=pending", headers=h).json()
    assert [p["id"] for p in listed["items"]] == [mine] and len(listed["items"][0]["items"]) == 1

    seller = _user(client, h, f, "Vendedor")
    assert client.get("/api/v1/purchases", headers=seller).status_code == 403
    assert _buy(client, seller, f, sup, [{"product_id": a, "quantity": 1, "unit_cost": "1.00"}]).status_code == 403
    manager = _user(client, h, f, "Gerente")
    assert client.get(f"/api/v1/purchases/{mine}", headers=manager).status_code == 200


def test_the_last_purchase_by_date_sets_the_cost(client, admin_headers):
    """An old offline purchase synced late adds stock but doesn't overwrite a newer cost."""
    h, f = admin_headers, _filial(client, admin_headers)
    a, sup = _product(client, h, f), _supplier(client, h, f)
    _buy(client, h, f, sup, [{"product_id": a, "quantity": 1, "unit_cost": "8.00"}], receive=True)  # today
    two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    pushed = client.post(f"/api/v1/sync/push?filial_id={f}", json={"purchases": [{
        "client_reference": str(uuid.uuid4()), "supplier_id": sup, "occurred_at": two_days_ago,
        "items": [{"product_id": a, "quantity": 5, "unit_cost": "6.00"}]}]}, headers=h).json()
    assert pushed["results"] == [] and pushed["purchases"][0]["status"] == "created"
    assert _stock(client, h, a) == 6 and _cost(client, h, a) == Decimal("8.00")  # stock yes, cost no

    client.put(f"/api/v1/catalog/products/{a}/cost", json={"cost_value": "9.00"}, headers=h)  # manual: as of now
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    client.post(f"/api/v1/sync/push?filial_id={f}", json={"purchases": [{
        "client_reference": str(uuid.uuid4()), "supplier_id": sup, "occurred_at": yesterday,
        "items": [{"product_id": a, "quantity": 1, "unit_cost": "7.00"}]}]}, headers=h)
    assert _cost(client, h, a) == Decimal("9.00")  # older than the manual cost


# ---------------------------------------------------------------------
# offline purchases (sync push)
# ---------------------------------------------------------------------


def test_offline_purchase_idempotency_and_mixed_batch(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    a, sup = _product(client, h, f, stock=5), _supplier(client, h, f)
    buyer = client.post(f"/api/v1/clients?filial_id={f}", json={"first_name": "C", "last_name": "D",
                        "email": f"{_u()}@example.com", "cpf": _digits(11)}, headers=h).json()["id"]
    now = datetime.now(timezone.utc).isoformat()
    purchase = {"client_reference": str(uuid.uuid4()), "supplier_id": sup, "occurred_at": now,
                "items": [{"product_id": a, "quantity": 10, "unit_cost": "2.00"}], "invoice_number": "999"}
    sale = {"client_reference": str(uuid.uuid4()), "client_id": buyer, "payment_method": "pix", "occurred_at": now,
            "items": [{"product_id": a, "quantity": 1}]}
    first = client.post(f"/api/v1/sync/push?filial_id={f}", json={"sales": [sale], "purchases": [purchase]}, headers=h).json()
    assert first["results"][0]["status"] == "created" and first["purchases"][0]["status"] == "created"
    created = client.get(f"/api/v1/purchases/{first['purchases'][0]['purchase_id']}", headers=h).json()
    assert created["status"] == "received" and created["client_reference"] == purchase["client_reference"]

    again = client.post(f"/api/v1/sync/push?filial_id={f}", json={"purchases": [purchase]}, headers=h).json()
    assert again["purchases"][0]["status"] == "duplicate"
    changed = {**purchase, "items": [{"product_id": a, "quantity": 99, "unit_cost": "2.00"}]}
    assert client.post(f"/api/v1/sync/push?filial_id={f}", json={"purchases": [changed]},
                       headers=h).json()["purchases"][0]["status"] == "conflict"
    assert _stock(client, h, a) == 5 + 10 - 1  # counted once

    rejected = {**purchase, "client_reference": str(uuid.uuid4()), "supplier_id": buyer}  # a client, not a supplier
    assert client.post(f"/api/v1/sync/push?filial_id={f}", json={"purchases": [rejected]},
                       headers=h).json()["purchases"][0]["status"] == "rejected"


def test_a_seller_cannot_push_purchases_but_their_sales_go_through(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    a, sup = _product(client, h, f, stock=3), _supplier(client, h, f)
    buyer = client.post(f"/api/v1/clients?filial_id={f}", json={"first_name": "C", "last_name": "D",
                        "email": f"{_u()}@example.com", "cpf": _digits(11)}, headers=h).json()["id"]
    seller = _user(client, h, f, "Vendedor")
    now = datetime.now(timezone.utc).isoformat()
    body = {"sales": [{"client_reference": str(uuid.uuid4()), "client_id": buyer, "payment_method": "cash",
                       "occurred_at": now, "items": [{"product_id": a, "quantity": 1}]}],
            "purchases": [{"client_reference": str(uuid.uuid4()), "supplier_id": sup, "occurred_at": now,
                           "items": [{"product_id": a, "quantity": 10, "unit_cost": "1.00"}]}]}
    resp = client.post(f"/api/v1/sync/push?filial_id={f}", json=body, headers=seller).json()
    assert resp["results"][0]["status"] == "created"
    assert resp["purchases"][0] == {"client_reference": body["purchases"][0]["client_reference"], "status": "rejected",
                                    "order_id": None, "purchase_id": None, "reason": "Missing permission purchases.manage"}
    assert _stock(client, h, a) == 2


def test_purchases_are_audited(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    a, sup = _product(client, h, f), _supplier(client, h, f)
    pid = _buy(client, h, f, sup, [{"product_id": a, "quantity": 1, "unit_cost": "1.00"}]).json()["id"]
    client.post(f"/api/v1/purchases/{pid}/receive", headers=h)
    client.post(f"/api/v1/purchases/{pid}/cancel", headers=h)
    actions = [r["action"] for r in client.get(f"/api/v1/audit-logs?entity_type=purchase&entity_id={pid}",
                                               headers=h).json()["items"]]
    assert actions == ["purchase.cancel", "purchase.receive", "purchase.create"]
