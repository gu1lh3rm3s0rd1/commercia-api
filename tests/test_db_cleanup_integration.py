"""Migration 0017 (second review): effective_at is required, and dropping the single-column
FKs left no gap — the composite FKs from 0016 still refuse a row pointing at nothing.
Require migrations up to 0017; skipped otherwise."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.session import engine
from tests.conftest import requires_db


def _cleanup_applied() -> bool:
    try:
        with engine.connect() as conn:
            return conn.execute(text("""SELECT is_nullable FROM information_schema.columns
                                        WHERE table_name = 'product_costs' AND column_name = 'effective_at'""")).scalar() == "NO"
    except Exception:
        return False


pytestmark = [requires_db, pytest.mark.skipif(not _cleanup_applied(), reason="migration 0017 not applied")]

MISSING = 999_999_999


def _refused(sql: str, **params) -> None:
    with engine.connect() as conn:
        tx = conn.begin()
        try:
            with pytest.raises(DBAPIError):
                conn.execute(text(sql), params)
        finally:
            tx.rollback()


def _a_product() -> tuple[int, int]:
    with engine.connect() as conn:
        return conn.execute(text("SELECT id, filial_id FROM products ORDER BY id LIMIT 1")).one()


def test_a_cost_needs_its_effective_date():
    product_id, _ = _a_product()
    _refused("INSERT INTO product_costs (product_id, cost_value) VALUES (:p, 1)", p=product_id)


def test_rows_pointing_at_nothing_are_still_refused():
    _, filial = _a_product()
    _refused("INSERT INTO stocks (product_id, filial_id, quantity) VALUES (:p, :f, 1)", p=MISSING, f=filial)
    _refused("""INSERT INTO prices (product_id, filial_id, price_default, price_offer, start_date)
                VALUES (:p, :f, 1, 0, '2020-01-01')""", p=MISSING, f=filial)
    _refused("""INSERT INTO stock_movements (product_id, filial_id, change_qty, reason, resulting_qty)
                VALUES (:p, :f, 1, 'restock', 1)""", p=MISSING, f=filial)
    _refused("UPDATE products SET category_id = :c WHERE id = (SELECT min(id) FROM products)", c=MISSING)
    _refused("UPDATE orders SET client_id = :c WHERE id = (SELECT min(id) FROM orders)", c=MISSING)
