"""Regression tests for the 2026-11-07 API audit — each one reproduces a defect the audit
confirmed against the dev database, so it can't come back unnoticed. Require a real
Postgres with the schema applied and scripts/seed_admin.py run; skipped otherwise.

Isolation tests create a second filial and a non-superuser user directly in the DB (same
approach as scripts/seed_admin.py — there are no routes to create them)."""

import random
import string
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.core.security import hash_password
from app.core.timezone import local_today, local_tz
from app.db.session import SessionLocal, engine
from app.models.company import Company
from app.models.user import Group, GroupPermission, Permission, User, UserCompany, UserGroup
from app.modules.sales import service as sales_service
from app.modules.sales.schemas import OrderCreate
from tests.conftest import requires_db

pytestmark = requires_db

PAST = "2020-01-01"


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _admin_filial_id(client, h) -> int:
    return client.get("/api/v1/auth/me", headers=h).json()["accessible_filial_ids"][0]


def _make_product(client, h, filial_id: int, price: str = "10.00", qty: int = 10, offer: str | None = None) -> int:
    department_id = client.post("/api/v1/catalog/departments", json={"name": _unique("Depto")}, headers=h).json()["id"]
    category_id = client.post(f"/api/v1/catalog/categories?filial_id={filial_id}",
                              json={"name": _unique("Categoria"), "department_id": department_id}, headers=h).json()["id"]
    product_id = client.post(f"/api/v1/catalog/products?filial_id={filial_id}",
                             json={"sku": _unique("SKU"), "name": "Produto auditoria", "category_id": category_id},
                             headers=h).json()["id"]
    body = {"price_default": price, "start_date": PAST}
    if offer:
        body["price_offer"] = offer
    assert client.post(f"/api/v1/catalog/products/{product_id}/prices", json=body, headers=h).status_code == 201
    client.put(f"/api/v1/catalog/products/{product_id}/cost", json={"cost_value": "4.00"}, headers=h)
    if qty:
        client.post(f"/api/v1/catalog/products/{product_id}/stock/adjustments",
                    json={"quantity_delta": qty, "reason": "restock"}, headers=h)
    return product_id


def _make_client(client, h, filial_id: int) -> int:
    resp = client.post(f"/api/v1/clients?filial_id={filial_id}", json={
        "first_name": "Audit", "last_name": "Cliente", "email": f"{_unique('a')}@example.com",
        "cpf": "".join(random.choices(string.digits, k=11)),
    }, headers=h)
    assert resp.status_code == 201
    return resp.json()["id"]


def _stock(client, h, product_id: int) -> int:
    return client.get(f"/api/v1/catalog/products/{product_id}/stock", headers=h).json()["quantity"]


def _movements_sum(product_id: int) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT coalesce(sum(change_qty), 0) FROM stock_movements WHERE product_id = :p"),
                            {"p": product_id}).scalar()


def _order(client, h, filial_id, client_id, items, **extra):
    return client.post(f"/api/v1/orders?filial_id={filial_id}", headers=h,
                       json={"client_id": client_id, "payment_method": "cash", "items": items, **extra})


def _push(client, h, filial_id, sale: dict):
    return client.post(f"/api/v1/sync/push?filial_id={filial_id}", json={"sales": [sale]}, headers=h)


def _sale(client_id, product_id, qty=1, occurred_at=None, reference=None):
    return {
        "client_reference": reference or str(uuid.uuid4()),
        "client_id": client_id,
        "payment_method": "pix",
        "occurred_at": occurred_at or datetime.now(local_tz()).isoformat(),
        "items": [{"product_id": product_id, "quantity": qty}],
    }


# ---------------------------------------------------------------------
# orders <-> items, totals, single-transaction behavior
# ---------------------------------------------------------------------


def test_multi_item_order_is_identical_in_create_detail_and_list(client, admin_headers):
    f = _admin_filial_id(client, admin_headers)
    a, b = _make_product(client, admin_headers, f, "10.00"), _make_product(client, admin_headers, f, "7.50", offer="6.00")
    cid = _make_client(client, admin_headers, f)
    created = _order(client, admin_headers, f, cid, [{"product_id": a, "quantity": 2}, {"product_id": b, "quantity": 3}],
                     discount="1.50")
    assert created.status_code == 201
    order = created.json()
    assert Decimal(order["total"]) == Decimal("2") * 10 + Decimal("3") * 6 - Decimal("1.50")  # offer used for b

    detail = client.get(f"/api/v1/orders/{order['id']}", headers=admin_headers).json()
    assert detail == order  # same values whether just created or read back
    with engine.connect() as conn:
        linked = conn.execute(text("SELECT id FROM order_items WHERE order_id = :o ORDER BY id"), {"o": order["id"]}).scalars().all()
        stored_total = conn.execute(text("SELECT total FROM orders WHERE id = :o"), {"o": order["id"]}).scalar()
    assert linked == sorted(i["id"] for i in order["items"])  # items reference orders.id via order_id
    assert stored_total == Decimal(order["total"])
    assert stored_total == sum(Decimal(i["total"]) for i in order["items"]) - Decimal(order["discount"])


def test_failed_order_writes_nothing(client, admin_headers):
    f = _admin_filial_id(client, admin_headers)
    ok, short = _make_product(client, admin_headers, f, qty=10), _make_product(client, admin_headers, f, qty=1)
    cid = _make_client(client, admin_headers, f)
    with engine.connect() as conn:
        orders_before = conn.execute(text("SELECT count(*) FROM orders")).scalar()
    resp = _order(client, admin_headers, f, cid, [{"product_id": ok, "quantity": 2}, {"product_id": short, "quantity": 5}])
    assert resp.status_code == 400
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM orders")).scalar() == orders_before
    assert _stock(client, admin_headers, ok) == 10 and _movements_sum(ok) == 10  # first line didn't half-happen


def test_same_product_on_two_lines_must_fit_the_stock_together(client, admin_headers):
    f = _admin_filial_id(client, admin_headers)
    p = _make_product(client, admin_headers, f, qty=10)
    cid = _make_client(client, admin_headers, f)
    over = _order(client, admin_headers, f, cid, [{"product_id": p, "quantity": 6}, {"product_id": p, "quantity": 6}])
    assert over.status_code == 400 and "requested 12" in over.json()["detail"]

    fits = _order(client, admin_headers, f, cid, [{"product_id": p, "quantity": 6}, {"product_id": p, "quantity": 4}])
    assert fits.status_code == 201
    assert _stock(client, admin_headers, p) == 0 == _movements_sum(p)
    with engine.connect() as conn:
        running = conn.execute(text("SELECT resulting_qty FROM stock_movements WHERE product_id = :p ORDER BY id"),
                               {"p": p}).scalars().all()
    assert running == [10, 4, 0]  # restock, then each line's running stock


# ---------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------


def _parallel(fn, n):
    barrier, results = threading.Barrier(n), []

    def run():
        db = SessionLocal()
        try:
            barrier.wait()
            fn(db)
            results.append("ok")
        except Exception as exc:  # noqa: BLE001 — the test counts outcomes
            results.append(getattr(exc, "status_code", type(exc).__name__))
        finally:
            db.close()

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_simultaneous_sales_never_oversell_nor_lose_stock_updates(client, admin_headers):
    f = _admin_filial_id(client, admin_headers)
    p = _make_product(client, admin_headers, f, qty=10)
    cid = _make_client(client, admin_headers, f)
    results = _parallel(lambda db: sales_service.create_order(
        db, f, 1, OrderCreate(client_id=cid, payment_method="cash", items=[{"product_id": p, "quantity": 1}])), 12)
    assert results.count("ok") == 10 and results.count(400) == 2
    assert _stock(client, admin_headers, p) == 0 == _movements_sum(p)


def test_simultaneous_cancellations_return_the_stock_once(client, admin_headers):
    f = _admin_filial_id(client, admin_headers)
    p = _make_product(client, admin_headers, f, qty=10)
    cid = _make_client(client, admin_headers, f)
    order_id = _order(client, admin_headers, f, cid, [{"product_id": p, "quantity": 4}]).json()["id"]

    def cancel(db):
        sales_service.cancel_order(db, sales_service.get_order_or_404(db, order_id, {f}))

    results = _parallel(cancel, 5)
    assert results.count("ok") == 1 and results.count(409) == 4
    assert _stock(client, admin_headers, p) == 10 == _movements_sum(p)


# ---------------------------------------------------------------------
# prices
# ---------------------------------------------------------------------


def test_offline_sale_is_priced_as_of_the_day_it_happened(client, admin_headers):
    f = _admin_filial_id(client, admin_headers)
    p = _make_product(client, admin_headers, f, price="10.00")
    cid = _make_client(client, admin_headers, f)
    client.post(f"/api/v1/catalog/products/{p}/prices", json={"price_default": "15.00"}, headers=admin_headers)  # from today

    two_days_ago = datetime.now(local_tz()) - timedelta(days=2)
    result = _push(client, admin_headers, f, _sale(cid, p, occurred_at=two_days_ago.isoformat())).json()["results"][0]
    assert result["status"] == "created"
    order = client.get(f"/api/v1/orders/{result['order_id']}", headers=admin_headers).json()
    assert Decimal(order["items"][0]["unit_price"]) == Decimal("10.00")

    online = _order(client, admin_headers, f, cid, [{"product_id": p, "quantity": 1}]).json()
    assert Decimal(online["items"][0]["unit_price"]) == Decimal("15.00")


def test_price_cannot_start_before_the_current_one(client, admin_headers):
    f = _admin_filial_id(client, admin_headers)
    p = _make_product(client, admin_headers, f)  # current price starts in 2020
    resp = client.post(f"/api/v1/catalog/products/{p}/prices",
                       json={"price_default": "12.00", "start_date": "2019-06-01"}, headers=admin_headers)
    assert resp.status_code == 422  # was an unhandled 500 (ck_prices_date_range)


# ---------------------------------------------------------------------
# request validation (contract)
# ---------------------------------------------------------------------


def test_sale_contract_rejects_what_it_would_otherwise_misrecord(client, admin_headers):
    f = _admin_filial_id(client, admin_headers)
    p = _make_product(client, admin_headers, f)
    cid = _make_client(client, admin_headers, f)
    item = [{"product_id": p, "quantity": 1}]

    # a price sent by the app was silently ignored (and a different one recorded)
    assert _order(client, admin_headers, f, cid, [{"product_id": p, "quantity": 1, "unit_price": "1.00"}]).status_code == 422
    # online sales can't carry offline-only fields
    assert _order(client, admin_headers, f, cid, item, occurred_at="2026-01-01T10:00:00-03:00").status_code == 422
    # 3 decimals: stored rounded, total no longer equal to items - discount
    assert _order(client, admin_headers, f, cid, item, discount="0.005").status_code == 422
    assert client.post(f"/api/v1/catalog/products/{p}/prices", json={"price_default": "9.999"},
                       headers=admin_headers).status_code == 422

    naive = _sale(cid, p, occurred_at="2026-11-05T10:00:00")
    future = _sale(cid, p, occurred_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    unknown = {**_sale(cid, p), "seller_name": "x"}
    for bad in (naive, future, unknown):
        assert _push(client, admin_headers, f, bad).status_code == 422, bad


def test_inactive_client_cannot_buy(client, admin_headers):
    f = _admin_filial_id(client, admin_headers)
    p = _make_product(client, admin_headers, f)
    cid = _make_client(client, admin_headers, f)
    client.delete(f"/api/v1/clients/{cid}", headers=admin_headers)
    resp = _order(client, admin_headers, f, cid, [{"product_id": p, "quantity": 1}])
    assert resp.status_code == 400 and "inactive" in resp.json()["detail"]


# ---------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------


def test_resent_reference_is_duplicate_only_when_it_is_the_same_sale(client, admin_headers):
    f = _admin_filial_id(client, admin_headers)
    p = _make_product(client, admin_headers, f)
    cid = _make_client(client, admin_headers, f)
    sale = _sale(cid, p, qty=1)
    first = _push(client, admin_headers, f, sale).json()["results"][0]
    again = _push(client, admin_headers, f, sale).json()["results"][0]
    other = _push(client, admin_headers, f, {**sale, "items": [{"product_id": p, "quantity": 3}]}).json()["results"][0]
    assert first["status"] == "created"
    assert again == {**first, "status": "duplicate"}
    assert other["status"] == "conflict" and other["order_id"] == first["order_id"]
    assert _stock(client, admin_headers, p) == 9  # nothing written by the duplicate/conflict


def test_stock_change_reaches_the_incremental_pull(client, admin_headers):
    f = _admin_filial_id(client, admin_headers)
    p = _make_product(client, admin_headers, f, qty=5)
    with engine.begin() as conn:  # an "old" product, outside the pull's overlap window
        for sql in ("UPDATE products SET updated_at = now() - interval '1 hour' WHERE id = :p",
                    "UPDATE prices SET created_at = now() - interval '1 hour' WHERE product_id = :p",
                    "UPDATE stocks SET updated_at = now() - interval '1 hour' WHERE product_id = :p",
                    "UPDATE product_costs SET updated_at = now() - interval '1 hour' WHERE product_id = :p"):
            conn.execute(text(sql), {"p": p})
    checkpoint = client.get(f"/api/v1/sync/pull?filial_id={f}", headers=admin_headers).json()["synced_at"]
    client.post(f"/api/v1/catalog/products/{p}/stock/adjustments", json={"quantity_delta": 3, "reason": "restock"},
                headers=admin_headers)

    pulled = client.get(f"/api/v1/sync/pull?filial_id={f}&last_synced_at={checkpoint.replace('+', '%2B')}",
                        headers=admin_headers).json()
    assert any(s["product_id"] == p and s["quantity"] == 8 for s in pulled["stocks"])
    product = next(x for x in pulled["products"] if x["id"] == p)  # re-sent with the fresh embedded stock
    assert product["stock"]["quantity"] == 8


# ---------------------------------------------------------------------
# isolation between filiais
# ---------------------------------------------------------------------


@pytest.fixture
def two_filiais(client, admin_headers):
    """Filial 'AUDIT2' (other store) + a regular user with access to the admin's filial only."""
    main = _admin_filial_id(client, admin_headers)
    db = SessionLocal()
    try:
        other = db.execute(select(Company).where(Company.filial_code == "AUDIT2")).scalar_one_or_none()
        if other is None:
            other = Company(filial_code="AUDIT2", corporate_name="Loja Auditoria 2 LTDA", cnpj="99999999000199")
            db.add(other)
            db.flush()
        username = _unique("gerente")
        user = User(username=username, full_name=username, email=f"{username}@example.com", password_hash=hash_password("senha123"))
        group = Group(name=_unique("Gerente"))
        db.add_all([user, group])
        db.flush()
        for code in ("orders.manage", "clients.edit", "products.edit", "users.manage"):
            perm = db.execute(select(Permission).where(Permission.code == code)).scalar_one()
            db.add(GroupPermission(group_id=group.id, permission_id=perm.id))
        db.add_all([UserGroup(user_id=user.id, group_id=group.id), UserCompany(user_id=user.id, filial_id=main)])
        other_user = User(username=_unique("outra"), full_name="Outra", email=f"{_unique('o')}@example.com", password_hash=hash_password("x"))
        db.add(other_user)
        db.flush()
        db.add(UserCompany(user_id=other_user.id, filial_id=other.id))
        db.commit()
        other_id, other_username = other.id, other_user.username
    finally:
        db.close()

    token = client.post("/api/v1/auth/login", data={"username": username, "password": "senha123"}).json()["access_token"]
    other_product = _make_product(client, admin_headers, other_id)
    other_client = _make_client(client, admin_headers, other_id)
    other_order = _order(client, admin_headers, other_id, other_client, [{"product_id": other_product, "quantity": 1}]).json()["id"]
    return {"main": main, "other": other_id, "h": {"Authorization": f"Bearer {token}"},
            "other_order": other_order, "other_product": other_product, "other_username": other_username}


def test_a_store_user_never_sees_or_writes_another_stores_data(client, two_filiais):
    t, h = two_filiais, two_filiais["h"]
    assert client.get(f"/api/v1/orders/{t['other_order']}", headers=h).status_code == 404
    assert all(o["filial_id"] == t["main"] for o in client.get("/api/v1/orders?limit=200", headers=h).json()["items"])
    assert client.get(f"/api/v1/orders?filial_id={t['other']}", headers=h).status_code == 403
    assert client.get(f"/api/v1/dashboard/reports/sales-summary?filial_id={t['other']}", headers=h).status_code == 403
    assert client.get(f"/api/v1/catalog/products/{t['other_product']}", headers=h).status_code == 403
    assert client.post(f"/api/v1/catalog/products/{t['other_product']}/stock/adjustments",
                       json={"quantity_delta": 1, "reason": "restock"}, headers=h).status_code == 403
    usernames = {u["username"] for u in client.get("/api/v1/users?limit=200", headers=h).json()["items"]}
    assert t["other_username"] not in usernames


def test_a_store_cannot_sell_another_stores_product_or_client(client, admin_headers, two_filiais):
    t = two_filiais
    own_client = _make_client(client, admin_headers, t["main"])
    resp = _order(client, admin_headers, t["main"], own_client, [{"product_id": t["other_product"], "quantity": 1}])
    assert resp.status_code == 400
    with engine.connect() as conn:
        mixed = conn.execute(text("""SELECT count(*) FROM order_items i JOIN orders o ON o.id = i.order_id
                                     JOIN products p ON p.id = i.product_id WHERE p.filial_id <> o.filial_id""")).scalar()
    assert mixed == 0


def test_reports_tell_the_same_revenue(client, admin_headers):
    """Same period, same filial: every report that shows revenue must agree (A4). Strict
    once order items carry their share of the order discount (migration 0009)."""
    f = _admin_filial_id(client, admin_headers)
    p = _make_product(client, admin_headers, f, "20.00")
    cid = _make_client(client, admin_headers, f)
    _order(client, admin_headers, f, cid, [{"product_id": p, "quantity": 2}], discount="3.00")
    day = local_today().isoformat()
    period = f"filial_id={f}&start_date={day}&end_date={day}"
    summary = client.get(f"/api/v1/dashboard/reports/sales-summary?{period}", headers=admin_headers).json()
    margin = client.get(f"/api/v1/dashboard/reports/margin-summary?{period}", headers=admin_headers).json()
    categories = client.get(f"/api/v1/dashboard/reports/sales-by-category?{period}&limit=200", headers=admin_headers).json()
    revenue = Decimal(summary["total_revenue"])
    assert Decimal(margin["total_revenue"]) == revenue
    assert sum(Decimal(c["total_revenue"]) for c in categories["items"]) == revenue
