"""User management (RF01/UC002) + audit trail (RNF11) — migration 0010. Require a real
Postgres with the schema applied and scripts/seed_admin.py run; skipped otherwise."""

import json
import random
import string
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.company import Company
from tests.conftest import requires_db

pytestmark = requires_db

PAST = "2020-01-01"
PROFILE_CODES = {
    "Administrador": {"users.manage", "products.edit", "clients.edit", "orders.manage", "audit.view",
                      "purchases.manage"},
    "Gerente": {"products.edit", "clients.edit", "orders.manage", "purchases.manage"},
    "Vendedor": {"orders.manage", "clients.edit"},
}


def _u(prefix: str = "u") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _login(client, username: str, password: str):
    return client.post("/api/v1/auth/login", data={"username": username, "password": password})


def _headers(client, username: str, password: str) -> dict[str, str]:
    resp = _login(client, username, password)
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _main_filial(client, h) -> int:
    return client.get("/api/v1/auth/me", headers=h).json()["accessible_filial_ids"][0]


def _group_ids(client, h) -> dict[str, int]:
    return {g["name"]: g["id"] for g in client.get("/api/v1/users/groups", headers=h).json()}


def _new_user(client, h, filial_ids: list[int], profile: str, password: str = "senha123", **extra):
    username = extra.pop("username", _u())
    body = {"full_name": f"Pessoa {username}", "username": username, "email": f"{username}@example.com",
            "password": password, "group_ids": [_group_ids(client, h)[profile]], "filial_ids": filial_ids, **extra}
    return client.post("/api/v1/users", json=body, headers=h)


def _logs(client, h, **params) -> list[dict]:
    resp = client.get("/api/v1/audit-logs", params={"limit": 200, **params}, headers=h)
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


@pytest.fixture
def stores(client, admin_headers):
    """The admin's filial + a second store 'AUDIT2', and a store administrator (fixed
    'Administrador' profile, created through the API) who only has the main filial."""
    main = _main_filial(client, admin_headers)
    db = SessionLocal()
    try:
        other = db.execute(select(Company).where(Company.filial_code == "AUDIT2")).scalar_one_or_none()
        if other is None:
            other = Company(filial_code="AUDIT2", corporate_name="Loja Auditoria 2 LTDA", cnpj="99999999000199")
            db.add(other)
            db.commit()
        other_id = other.id
    finally:
        db.close()
    resp = _new_user(client, admin_headers, [main], "Administrador")
    assert resp.status_code == 201, resp.text
    store_admin = resp.json()
    return {"main": main, "other": other_id, "store_admin": store_admin,
            "h": _headers(client, store_admin["username"], "senha123")}


# ---------------------------------------------------------------------
# fixed profiles
# ---------------------------------------------------------------------


def test_the_three_fixed_profiles_exist_with_their_permissions(client, admin_headers):
    groups = {g["name"]: set(g["permissions"]) for g in client.get("/api/v1/users/groups", headers=admin_headers).json()}
    for name, codes in PROFILE_CODES.items():
        assert groups[name] == codes
    codes = {p["code"] for p in client.get("/api/v1/users/permissions", headers=admin_headers).json()}
    assert set().union(*PROFILE_CODES.values()) <= codes


def test_a_seller_can_sell_but_not_touch_the_catalog(client, admin_headers, stores):
    seller = _new_user(client, admin_headers, [stores["main"]], "Vendedor").json()
    h = _headers(client, seller["username"], "senha123")
    f = stores["main"]
    created = client.post(f"/api/v1/clients?filial_id={f}", json={
        "first_name": "Cli", "last_name": "Vend", "email": f"{_u('c')}@example.com",
        "cpf": "".join(random.choices(string.digits, k=11))}, headers=h)
    assert created.status_code == 201
    assert client.post(f"/api/v1/catalog/categories?filial_id={f}", json={"name": _u(), "department_id": 1},
                       headers=h).status_code == 403
    assert client.get("/api/v1/users", headers=h).status_code == 403
    assert client.get("/api/v1/audit-logs", headers=h).status_code == 403


# ---------------------------------------------------------------------
# CRUD + UC002 alternative flows
# ---------------------------------------------------------------------


def test_create_read_and_login_a_new_user(client, admin_headers, stores):
    resp = _new_user(client, admin_headers, [stores["main"]], "Gerente")
    assert resp.status_code == 201
    user = resp.json()
    assert user["full_name"].startswith("Pessoa ")
    assert [g["name"] for g in user["groups"]] == ["Gerente"]
    assert user["accessible_filial_ids"] == [stores["main"]]
    assert user["is_active"] is True and user["is_superuser"] is False and user["last_login"] is None

    before = datetime.now(timezone.utc)
    _headers(client, user["username"], "senha123")
    fetched = client.get(f"/api/v1/users/{user['id']}", headers=admin_headers).json()
    assert datetime.fromisoformat(fetched["last_login"]) >= before.replace(microsecond=0)


def test_duplicates_and_invalid_data_are_refused(client, admin_headers, stores):
    f = [stores["main"]]
    first = _new_user(client, admin_headers, f, "Vendedor").json()
    dup_name = _new_user(client, admin_headers, f, "Vendedor", username=first["username"].upper())
    assert dup_name.status_code == 409 and "Username" in dup_name.json()["detail"]
    other = _u()
    dup_mail = client.post("/api/v1/users", json={
        "full_name": "X", "username": other, "email": first["email"], "password": "senha123",
        "group_ids": [_group_ids(client, admin_headers)["Vendedor"]], "filial_ids": f}, headers=admin_headers)
    assert dup_mail.status_code == 409 and "E-mail" in dup_mail.json()["detail"]

    base = {"full_name": "Fulano", "username": _u(), "email": f"{_u()}@example.com", "password": "senha123",
            "group_ids": [_group_ids(client, admin_headers)["Vendedor"]], "filial_ids": f}
    for broken in ({k: v for k, v in base.items() if k != "full_name"},  # required field missing
                   {**base, "password": "123"},                          # too short
                   {**base, "username": "com espaço"},
                   {**base, "email": "sem-arroba"},
                   {**base, "group_ids": []},
                   {**base, "is_superuser": True}):                      # unknown field
        assert client.post("/api/v1/users", json=broken, headers=admin_headers).status_code == 422
    assert client.post("/api/v1/users", json={**base, "group_ids": [987654]}, headers=admin_headers).status_code == 400


def test_update_deactivate_and_reactivate(client, admin_headers, stores):
    user = _new_user(client, admin_headers, [stores["main"]], "Vendedor").json()
    h_user = _headers(client, user["username"], "senha123")
    groups = _group_ids(client, admin_headers)

    patched = client.patch(f"/api/v1/users/{user['id']}", json={"full_name": "Nome Novo", "group_ids": [groups["Gerente"]]},
                           headers=admin_headers).json()
    assert patched["full_name"] == "Nome Novo" and [g["name"] for g in patched["groups"]] == ["Gerente"]

    gone = client.delete(f"/api/v1/users/{user['id']}", headers=admin_headers)
    assert gone.status_code == 200 and gone.json()["is_active"] is False
    assert _login(client, user["username"], "senha123").status_code == 401
    assert client.get("/api/v1/auth/me", headers=h_user).status_code == 401  # existing token stops working
    pulled = client.get(f"/api/v1/sync/pull?filial_id={stores['main']}", headers=admin_headers).json()["users"]
    assert next(u for u in pulled if u["id"] == user["id"])["is_active"] is False

    back = client.patch(f"/api/v1/users/{user['id']}", json={"is_active": True}, headers=admin_headers)
    assert back.json()["is_active"] is True
    assert _login(client, user["username"], "senha123").status_code == 200


def test_password_reset_and_own_password_change(client, admin_headers, stores):
    user = _new_user(client, admin_headers, [stores["main"]], "Vendedor").json()
    assert client.post(f"/api/v1/users/{user['id']}/password", json={"new_password": "trocada1"},
                       headers=admin_headers).status_code == 204
    assert _login(client, user["username"], "senha123").status_code == 401
    h = _headers(client, user["username"], "trocada1")

    assert client.post("/api/v1/auth/me/password", json={"current_password": "errada", "new_password": "outra123"},
                       headers=h).status_code == 400
    assert client.post("/api/v1/auth/me/password", json={"current_password": "trocada1", "new_password": "outra123"},
                       headers=h).status_code == 204
    assert _login(client, user["username"], "outra123").status_code == 200


# ---------------------------------------------------------------------
# store administrators are confined to their stores
# ---------------------------------------------------------------------


def test_store_admin_cannot_reach_other_stores_or_superusers(client, admin_headers, stores):
    h, main, other = stores["h"], stores["main"], stores["other"]
    outsider = _new_user(client, admin_headers, [other], "Vendedor").json()
    assert client.get(f"/api/v1/users/{outsider['id']}", headers=h).status_code == 404
    assert client.patch(f"/api/v1/users/{outsider['id']}", json={"full_name": "x"}, headers=h).status_code == 404
    assert _new_user(client, h, [other], "Vendedor").status_code == 403

    admin_id = next(u["id"] for u in client.get("/api/v1/users?limit=200", headers=admin_headers).json()["items"]
                    if u["username"] == "admin")
    assert client.get(f"/api/v1/users/{admin_id}", headers=h).status_code == 404  # superusers aren't visible

    # a user of both stores: editing the filial list can't drop the store the admin can't see
    shared = _new_user(client, admin_headers, [main, other], "Vendedor").json()
    kept = client.patch(f"/api/v1/users/{shared['id']}", json={"filial_ids": [main]}, headers=h).json()
    assert kept["accessible_filial_ids"] == sorted([main, other])


def test_store_admin_cannot_lock_themselves_out(client, stores):
    me = stores["store_admin"]
    h = stores["h"]
    assert client.delete(f"/api/v1/users/{me['id']}", headers=h).status_code == 400
    assert client.patch(f"/api/v1/users/{me['id']}", json={"is_active": False}, headers=h).status_code == 400
    seller_group = _group_ids(client, h)["Vendedor"]
    resp = client.patch(f"/api/v1/users/{me['id']}", json={"group_ids": [seller_group]}, headers=h)
    assert resp.status_code == 400
    assert client.get("/api/v1/users", headers=h).status_code == 200  # still an admin


# ---------------------------------------------------------------------
# audit trail
# ---------------------------------------------------------------------


def _product(client, h, f: int, qty: int = 5) -> int:
    dep = client.post("/api/v1/catalog/departments", json={"name": _u("D")}, headers=h).json()["id"]
    cat = client.post(f"/api/v1/catalog/categories?filial_id={f}", json={"name": _u("C"), "department_id": dep},
                      headers=h).json()["id"]
    pid = client.post(f"/api/v1/catalog/products?filial_id={f}", json={"sku": _u("S"), "name": "Prod log", "category_id": cat},
                      headers=h).json()["id"]
    client.post(f"/api/v1/catalog/products/{pid}/prices", json={"price_default": "10.00", "start_date": PAST}, headers=h)
    client.post(f"/api/v1/catalog/products/{pid}/stock/adjustments", json={"quantity_delta": qty, "reason": "restock"},
                headers=h)
    return pid


def _client(client, h, f: int) -> int:
    return client.post(f"/api/v1/clients?filial_id={f}", json={
        "first_name": "Log", "last_name": "Cli", "email": f"{_u('l')}@example.com",
        "cpf": "".join(random.choices(string.digits, k=11))}, headers=h).json()["id"]


def test_logins_are_logged_and_failures_too(client, admin_headers, stores):
    user = _new_user(client, admin_headers, [stores["main"]], "Vendedor").json()
    _login(client, user["username"], "errada")
    _login(client, user["username"], "senha123")
    rows = _logs(client, admin_headers, user_id=user["id"], action="auth.")
    assert [r["action"] for r in rows[:2]] == ["auth.login", "auth.login_failed"]
    assert rows[1]["details"]["username"] == user["username"]
    assert rows[0]["ip"]


def test_writes_are_logged_with_who_and_what_changed(client, stores):
    h, f, me = stores["h"], stores["main"], stores["store_admin"]
    pid = _product(client, h, f)
    client.patch(f"/api/v1/catalog/products/{pid}", json={"name": "Prod renomeado"}, headers=h)

    rows = _logs(client, h, entity_type="product", entity_id=pid)
    assert [r["action"] for r in rows] == ["product.update", "product.create"]
    assert rows[0]["details"] == {"name": ["Prod log", "Prod renomeado"]}
    assert rows[0]["username"] == me["username"] and rows[0]["filial_id"] == f
    stock = _logs(client, h, action="stock.adjust", filial_id=f)
    assert any(r["details"]["product_id"] == pid and r["details"]["change_qty"] == 5 for r in stock)
    assert any(r["details"]["product_id"] == pid for r in _logs(client, h, action="price.set", filial_id=f))


def test_orders_offline_sales_and_cancellations_are_logged(client, stores):
    h, f = stores["h"], stores["main"]
    pid, cid = _product(client, h, f), None
    cid = _client(client, h, f)
    online = client.post(f"/api/v1/orders?filial_id={f}", json={
        "client_id": cid, "payment_method": "cash", "items": [{"product_id": pid, "quantity": 1}]}, headers=h).json()
    ref = str(uuid.uuid4())
    pushed = client.post(f"/api/v1/sync/push?filial_id={f}", json={"sales": [{
        "client_reference": ref, "client_id": cid, "payment_method": "pix",
        "occurred_at": datetime.now(timezone.utc).isoformat(), "items": [{"product_id": pid, "quantity": 1}]}]},
        headers=h).json()["results"][0]
    client.post(f"/api/v1/orders/{online['id']}/cancel", headers=h)

    created = {r["entity_id"]: r for r in _logs(client, h, action="order.create", filial_id=f)}
    assert "source" not in created[online["id"]]["details"]
    assert created[pushed["order_id"]]["details"]["source"] == "sync"
    assert created[pushed["order_id"]]["details"]["client_reference"] == ref
    assert [r["action"] for r in _logs(client, h, entity_type="order", entity_id=online["id"])] == ["order.cancel", "order.create"]


def test_a_failed_operation_leaves_no_log(client, stores):
    h, f = stores["h"], stores["main"]
    pid, cid = _product(client, h, f, qty=1), _client(client, h, f)
    before = len(_logs(client, h, action="order.create", filial_id=f))
    resp = client.post(f"/api/v1/orders?filial_id={f}", json={
        "client_id": cid, "payment_method": "cash", "items": [{"product_id": pid, "quantity": 5}]}, headers=h)
    assert resp.status_code == 400
    assert len(_logs(client, h, action="order.create", filial_id=f)) == before


def test_user_management_is_logged_without_passwords(client, stores):
    h, f = stores["h"], stores["main"]
    user = _new_user(client, h, [f], "Vendedor", password="segredo99").json()
    client.post(f"/api/v1/users/{user['id']}/password", json={"new_password": "segredo77"}, headers=h)
    client.delete(f"/api/v1/users/{user['id']}", headers=h)
    rows = _logs(client, h, entity_type="user", entity_id=user["id"])
    assert [r["action"] for r in rows] == ["user.deactivate", "user.password_reset", "user.create"]
    dumped = json.dumps(rows)
    assert "segredo" not in dumped and "$2b$" not in dumped and "password_hash" not in dumped


def test_store_admin_only_sees_their_stores_logs(client, admin_headers, stores):
    h, other = stores["h"], stores["other"]
    _product(client, admin_headers, other)  # written by the superuser in the other store
    assert client.get(f"/api/v1/audit-logs?filial_id={other}", headers=h).status_code == 403
    rows = _logs(client, h)
    assert all(r["filial_id"] in (None, stores["main"]) for r in rows)
    assert any(r["filial_id"] == other for r in _logs(client, admin_headers, filial_id=other))
