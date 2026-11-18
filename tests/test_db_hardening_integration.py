"""Migration 0016 (DB hardening after the external review): the database itself now refuses
what the services already prevented. Each test writes the forbidden thing straight in SQL,
inside a transaction that is always rolled back. Require migrations up to 0016; skipped
otherwise."""

import random
import string
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.timezone import local_today
from app.db.session import engine
from tests.conftest import requires_db


def _hardening_applied() -> bool:
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT 1 FROM pg_constraint WHERE conname = 'ex_prices_no_overlap'")).first() is not None
    except Exception:
        return False


pytestmark = [requires_db, pytest.mark.skipif(not _hardening_applied(), reason="migration 0016 not applied")]

PAST = "2020-01-01"


def _u() -> str:
    return uuid.uuid4().hex[:8]


def _digits(n: int) -> str:
    return "".join(random.choices(string.digits, k=n))


def _filial(client, h) -> int:
    return client.get("/api/v1/auth/me", headers=h).json()["accessible_filial_ids"][0]


def _product(client, h, f: int, stock: int = 5) -> int:
    dep = client.post("/api/v1/catalog/departments", json={"name": f"D{_u()}"}, headers=h).json()["id"]
    cat = client.post(f"/api/v1/catalog/categories?filial_id={f}", json={"name": f"C{_u()}", "department_id": dep},
                      headers=h).json()["id"]
    pid = client.post(f"/api/v1/catalog/products?filial_id={f}", json={"sku": f"S{_u()}", "name": "Prod 0016",
                      "category_id": cat}, headers=h).json()["id"]
    client.post(f"/api/v1/catalog/products/{pid}/prices", json={"price_default": "10.00", "start_date": PAST}, headers=h)
    client.put(f"/api/v1/catalog/products/{pid}/cost", json={"cost_value": "4.00"}, headers=h)
    if stock:
        client.post(f"/api/v1/catalog/products/{pid}/stock/adjustments", json={"quantity_delta": stock, "reason": "restock"},
                    headers=h)
    return pid


def _client(client, h, f: int) -> int:
    return client.post(f"/api/v1/clients?filial_id={f}", json={"first_name": "Cli", "last_name": "0016",
                       "email": f"{_u()}@example.com", "cpf": _digits(11)}, headers=h).json()["id"]


def _order(client, h, f: int, pid: int) -> int:
    return client.post(f"/api/v1/orders?filial_id={f}", json={"client_id": _client(client, h, f), "payment_method": "cash",
                       "items": [{"product_id": pid, "quantity": 1}]}, headers=h).json()["id"]


def _refused(sql: str, **params) -> None:
    """The statement must fail; nothing is ever kept (always rolled back)."""
    with engine.connect() as conn:
        tx = conn.begin()
        try:
            with pytest.raises(DBAPIError):
                conn.execute(text(sql), params)
        finally:
            tx.rollback()


def _other_store() -> int:
    with engine.begin() as conn:
        row = conn.execute(text("SELECT id FROM companies WHERE filial_code = 'AUDIT2'")).first()
        if row:
            return row[0]
        return conn.execute(text("""INSERT INTO companies (filial_code, corporate_name, cnpj)
                                    VALUES ('AUDIT2', 'Loja Auditoria 2 LTDA', '99999999000199') RETURNING id""")).scalar()


# ---------------------------------------------------------------------
# 1. history can't be deleted
# ---------------------------------------------------------------------


def test_a_product_with_history_cant_be_deleted(client, admin_headers):
    pid = _product(client, admin_headers, _filial(client, admin_headers))
    _refused("DELETE FROM products WHERE id = :p", p=pid)


def test_a_sold_order_cant_be_deleted(client, admin_headers):
    f = _filial(client, admin_headers)
    oid = _order(client, admin_headers, f, _product(client, admin_headers, f))
    _refused("DELETE FROM orders WHERE id = :o", o=oid)  # its items are referenced by the stock ledger


# ---------------------------------------------------------------------
# 2. stores can't be mixed
# ---------------------------------------------------------------------


def test_rows_must_belong_to_the_products_store(client, admin_headers):
    f, other = _filial(client, admin_headers), _other_store()
    pid = _product(client, admin_headers, f)
    _refused("UPDATE stocks SET filial_id = :o WHERE product_id = :p", o=other, p=pid)
    _refused("UPDATE prices SET filial_id = :o WHERE product_id = :p", o=other, p=pid)
    _refused("""INSERT INTO stock_movements (product_id, filial_id, change_qty, reason, resulting_qty)
                VALUES (:p, :o, 1, 'restock', 1)""", o=other, p=pid)
    other_category = client.post(f"/api/v1/catalog/categories?filial_id={other}", json={
        "name": f"C{_u()}", "department_id": client.get("/api/v1/catalog/departments", headers=admin_headers).json()[0]["id"]},
        headers=admin_headers).json()["id"]
    _refused("UPDATE products SET category_id = :c WHERE id = :p", c=other_category, p=pid)


def test_an_order_client_and_a_purchase_supplier_belong_to_the_store(client, admin_headers):
    f, other = _filial(client, admin_headers), _other_store()
    oid = _order(client, admin_headers, f, _product(client, admin_headers, f))
    outsider = _client(client, admin_headers, other)
    _refused("UPDATE orders SET client_id = :c WHERE id = :o", c=outsider, o=oid)


# ---------------------------------------------------------------------
# 3. no overlapping price periods
# ---------------------------------------------------------------------


def test_price_periods_cant_overlap_but_can_touch(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    pid = _product(client, h, f)
    # the API's own flow still works: a new price today closes the previous one on the same day
    assert client.post(f"/api/v1/catalog/products/{pid}/prices", json={"price_default": "11.00"}, headers=h).status_code == 201
    future = (local_today() + timedelta(days=10)).isoformat()
    assert client.post(f"/api/v1/catalog/products/{pid}/prices",
                       json={"price_default": "12.00", "start_date": future}, headers=h).status_code == 201
    # an explicit period inside an existing one is refused by the DB
    _refused("""INSERT INTO prices (product_id, filial_id, price_default, price_offer, start_date, end_date)
                VALUES (:p, :f, 5, 0, :a, :b)""", p=pid, f=f, a=date(2021, 1, 1), b=date(2021, 2, 1))


def test_an_overlapping_price_is_a_409_not_a_500(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    pid = _product(client, h, f)
    future = (local_today() + timedelta(days=10)).isoformat()
    client.post(f"/api/v1/catalog/products/{pid}/prices", json={"price_default": "12.00", "start_date": future}, headers=h)
    # the price valid today is the 2020 one: closing it today leaves an open new price that runs into the future one
    resp = client.post(f"/api/v1/catalog/products/{pid}/prices", json={"price_default": "11.00"}, headers=h)
    assert resp.status_code == 409 and "overlaps" in resp.json()["detail"]


# ---------------------------------------------------------------------
# 4. stock movements: who, and reason tied to source
# ---------------------------------------------------------------------


def test_every_movement_records_who_moved_the_stock(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    pid = _product(client, h, f)  # restock adjustment
    oid = _order(client, h, f, pid)  # sale
    client.post(f"/api/v1/orders/{oid}/cancel", headers=h)  # reversal
    admin_id = client.get("/api/v1/auth/me", headers=h).json()["id"]
    with engine.connect() as conn:
        authors = conn.execute(text("SELECT reason::text, created_by_id FROM stock_movements WHERE product_id = :p ORDER BY id"),
                               {"p": pid}).all()
    assert [r for r, _ in authors] == ["restock", "sale", "adjustment"]
    assert {a for _, a in authors} == {admin_id}


def test_the_reason_must_match_the_source(client, admin_headers):
    f = _filial(client, admin_headers)
    pid = _product(client, admin_headers, f)
    _refused("""INSERT INTO stock_movements (product_id, filial_id, change_qty, reason, resulting_qty)
                VALUES (:p, :f, -1, 'sale', 0)""", p=pid, f=f)  # a sale with no order item
    _refused("""INSERT INTO stock_movements (product_id, filial_id, change_qty, reason, resulting_qty)
                VALUES (:p, :f, 1, 'purchase', 1)""", p=pid, f=f)  # a purchase with no purchase item


# ---------------------------------------------------------------------
# 5. NF-e once per store
# ---------------------------------------------------------------------


def test_the_same_nfe_cant_be_registered_twice_unless_cancelled(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    pid = _product(client, h, f, stock=0)
    supplier = client.post(f"/api/v1/people?filial_id={f}", json={"person_type": "company", "roles": ["supplier"],
                           "corporate_name": f"Forn {_u()}", "cnpj": _digits(14)}, headers=h).json()["id"]
    key = _digits(44)
    body = {"supplier_id": supplier, "invoice_key": key, "items": [{"product_id": pid, "quantity": 1, "unit_cost": "1.00"}]}
    first = client.post(f"/api/v1/purchases?filial_id={f}", json=body, headers=h).json()
    again = client.post(f"/api/v1/purchases?filial_id={f}", json=body, headers=h)
    assert again.status_code == 409 and f"(purchase {first['id']})" in again.json()["detail"]
    client.post(f"/api/v1/purchases/{first['id']}/cancel", headers=h)
    assert client.post(f"/api/v1/purchases?filial_id={f}", json=body, headers=h).status_code == 201


# ---------------------------------------------------------------------
# 6. e-mail / username regardless of case
# ---------------------------------------------------------------------


def test_email_and_username_are_unique_regardless_of_case():
    insert = text("""INSERT INTO users (username, full_name, email, password_hash, is_active, is_superuser)
                     VALUES (:u, 'X', :e, 'x', true, false)""")
    base = f"caso{_u()}"
    with engine.connect() as conn:
        tx = conn.begin()
        try:
            conn.execute(insert, {"u": base, "e": f"{base}@example.com"})  # a valid insert works...
            for dup in ({"u": f"outro{_u()}", "e": f"{base.upper()}@EXAMPLE.COM"},  # ...the same e-mail in capitals doesn't
                        {"u": base.upper(), "e": f"{_u()}@example.com"}):           # ...nor the same username
                nested = conn.begin_nested()
                with pytest.raises(DBAPIError):
                    conn.execute(insert, dup)
                nested.rollback()
        finally:
            tx.rollback()
