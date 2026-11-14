"""TCC requirement gaps closed on 2026-11-14: a client's purchase history on GET /orders
(RF03) and category "delete" = deactivate (RF04, migration 0011). Require a real Postgres
with the schema applied and scripts/seed_admin.py run; skipped otherwise."""

import random
import string
import uuid
from datetime import timedelta

from app.core.timezone import local_today
from tests.conftest import requires_db

pytestmark = requires_db

PAST = "2020-01-01"


def _u(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _filial(client, h) -> int:
    return client.get("/api/v1/auth/me", headers=h).json()["accessible_filial_ids"][0]


def _category(client, h, f: int) -> int:
    dep = client.post("/api/v1/catalog/departments", json={"name": _u("D")}, headers=h).json()["id"]
    return client.post(f"/api/v1/catalog/categories?filial_id={f}", json={"name": _u("C"), "department_id": dep},
                       headers=h).json()["id"]


def _product(client, h, f: int, category_id: int, qty: int = 10):
    resp = client.post(f"/api/v1/catalog/products?filial_id={f}",
                       json={"sku": _u("S"), "name": "Prod gaps", "category_id": category_id}, headers=h)
    if resp.status_code != 201:
        return resp
    pid = resp.json()["id"]
    client.post(f"/api/v1/catalog/products/{pid}/prices", json={"price_default": "10.00", "start_date": PAST}, headers=h)
    client.post(f"/api/v1/catalog/products/{pid}/stock/adjustments", json={"quantity_delta": qty, "reason": "restock"},
                headers=h)
    return resp


def _client(client, h, f: int) -> int:
    return client.post(f"/api/v1/clients?filial_id={f}", json={
        "first_name": "Hist", "last_name": "Cli", "email": f"{_u('h')}@example.com",
        "cpf": "".join(random.choices(string.digits, k=11))}, headers=h).json()["id"]


def _order(client, h, f: int, cid: int, items: list[dict]) -> dict:
    resp = client.post(f"/api/v1/orders?filial_id={f}", json={"client_id": cid, "payment_method": "cash", "items": items},
                       headers=h)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------
# RF03 — purchase history
# ---------------------------------------------------------------------


def test_orders_can_be_filtered_by_client_and_period(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    p1 = _product(client, h, f, _category(client, h, f)).json()["id"]
    p2 = _product(client, h, f, _category(client, h, f)).json()["id"]
    ana, bia = _client(client, h, f), _client(client, h, f)
    first = _order(client, h, f, ana, [{"product_id": p1, "quantity": 1}, {"product_id": p2, "quantity": 2}])
    second = _order(client, h, f, ana, [{"product_id": p2, "quantity": 1}])
    _order(client, h, f, bia, [{"product_id": p1, "quantity": 1}])

    history = client.get(f"/api/v1/orders?client_id={ana}", headers=h).json()
    assert history["total"] == 2
    assert [o["id"] for o in history["items"]] == [second["id"], first["id"]]
    # the list carries exactly the same items as the detail (items now fetched in one query)
    for order in history["items"]:
        detail = client.get(f"/api/v1/orders/{order['id']}", headers=h).json()
        assert order["items"] == detail["items"]

    today = local_today()
    assert client.get(f"/api/v1/orders?client_id={ana}&start_date={today}&end_date={today}", headers=h).json()["total"] == 2
    tomorrow = today + timedelta(days=1)
    assert client.get(f"/api/v1/orders?client_id={ana}&start_date={tomorrow}", headers=h).json()["total"] == 0
    assert client.get(f"/api/v1/orders?start_date={tomorrow}&end_date={today}", headers=h).status_code == 422


# ---------------------------------------------------------------------
# RF04 — category "delete" = deactivate
# ---------------------------------------------------------------------


def test_category_is_deactivated_only_without_active_products(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    cat = _category(client, h, f)
    pid = _product(client, h, f, cat).json()["id"]

    blocked = client.delete(f"/api/v1/catalog/categories/{cat}", headers=h)
    assert blocked.status_code == 409 and "1 active product" in blocked.json()["detail"]
    assert client.patch(f"/api/v1/catalog/categories/{cat}", json={"status": "inactive"}, headers=h).status_code == 409

    client.delete(f"/api/v1/catalog/products/{pid}", headers=h)  # product deactivated → category free
    gone = client.delete(f"/api/v1/catalog/categories/{cat}", headers=h)
    assert gone.status_code == 200 and gone.json()["status"] == "inactive"
    assert client.get(f"/api/v1/catalog/categories/{cat}", headers=h).json()["status"] == "inactive"
    assert client.delete(f"/api/v1/catalog/categories/{cat}", headers=h).status_code == 200  # idempotent

    actions = [r["action"] for r in client.get(
        f"/api/v1/audit-logs?entity_type=category&entity_id={cat}", headers=h).json()["items"]]
    assert actions[0] == "category.deactivate"


def test_no_product_goes_into_an_inactive_category(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    inactive = _category(client, h, f)
    client.delete(f"/api/v1/catalog/categories/{inactive}", headers=h)

    created = _product(client, h, f, inactive)
    assert created.status_code == 400 and created.json()["detail"] == "Category is inactive"
    pid = _product(client, h, f, _category(client, h, f)).json()["id"]
    moved = client.patch(f"/api/v1/catalog/products/{pid}", json={"category_id": inactive}, headers=h)
    assert moved.status_code == 400

    back = client.patch(f"/api/v1/catalog/categories/{inactive}", json={"status": "active"}, headers=h)
    assert back.json()["status"] == "active"
    assert client.patch(f"/api/v1/catalog/products/{pid}", json={"category_id": inactive}, headers=h).status_code == 200


def test_the_app_learns_about_the_deactivation_on_pull(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    cat = _category(client, h, f)
    synced_at = client.get(f"/api/v1/sync/pull?filial_id={f}", headers=h).json()["synced_at"]
    client.delete(f"/api/v1/catalog/categories/{cat}", headers=h)
    pulled = client.get(f"/api/v1/sync/pull", params={"filial_id": f, "last_synced_at": synced_at}, headers=h).json()
    assert next(c for c in pulled["categories"] if c["id"] == cat)["status"] == "inactive"


def test_a_seller_cannot_deactivate_categories(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    cat = _category(client, h, f)
    seller_group = next(g["id"] for g in client.get("/api/v1/users/groups", headers=h).json() if g["name"] == "Vendedor")
    username = _u("vend").replace("-", "")
    client.post("/api/v1/users", json={"full_name": "Vend", "username": username, "email": f"{username}@example.com",
                                        "password": "senha123", "group_ids": [seller_group], "filial_ids": [f]}, headers=h)
    token = client.post("/api/v1/auth/login", data={"username": username, "password": "senha123"}).json()["access_token"]
    resp = client.delete(f"/api/v1/catalog/categories/{cat}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
