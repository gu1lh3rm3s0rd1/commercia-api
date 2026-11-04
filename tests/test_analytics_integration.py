"""Integration tests for the Fase 7 analytics jobs — require a real Postgres with migrations
up to 0005 applied (analytics schema). Skipped automatically otherwise.

The analytics tables are derived, so these tests rebuild them freely. They still create
products/orders in `public` (no test-DB isolation in this project) — after running the
suite, re-run scripts.simulate_history --reset to get the clean simulated history back.
"""

import random
import string
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.analytics import jobs
from app.core.timezone import local_today, local_tz
from app.db.session import engine
from tests.conftest import requires_db


def _analytics_schema_applied() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM analytics.job_runs LIMIT 1"))
        return True
    except Exception:
        return False


pytestmark = [
    requires_db,
    pytest.mark.skipif(not _analytics_schema_applied(), reason="analytics schema not applied yet (migration 0005)"),
]


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _admin_filial_id(client, admin_headers) -> int:
    return client.get("/api/v1/auth/me", headers=admin_headers).json()["accessible_filial_ids"][0]


def _make_product(client, admin_headers, filial_id: int, qty: int) -> int:
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
        json={"sku": _unique("SKU"), "name": "Produto analytics", "category_id": category_id},
        headers=admin_headers,
    ).json()["id"]
    client.post(f"/api/v1/catalog/products/{product_id}/prices", json={"price_default": "10.00"}, headers=admin_headers)
    client.post(
        f"/api/v1/catalog/products/{product_id}/stock/adjustments",
        json={"quantity_delta": qty, "reason": "restock"},
        headers=admin_headers,
    )
    return product_id


def _make_client(client, admin_headers, filial_id: int) -> int:
    return client.post(
        f"/api/v1/clients?filial_id={filial_id}",
        json={
            "first_name": "Analytics",
            "last_name": "Teste",
            "email": f"{_unique('a')}@example.com",
            "cpf": "".join(random.choices(string.digits, k=11)),
        },
        headers=admin_headers,
    ).json()["id"]


def _push_sale(client, admin_headers, filial_id, client_id, product_id, qty, occurred_at: datetime) -> int:
    resp = client.post(
        f"/api/v1/sync/push?filial_id={filial_id}",
        json={"sales": [{
            "client_reference": str(uuid.uuid4()),
            "client_id": client_id,
            "payment_method": "pix",
            "occurred_at": occurred_at.isoformat(),
            "items": [{"product_id": product_id, "quantity": qty}],
        }]},
        headers=admin_headers,
    )
    result = resp.json()["results"][0]
    assert result["status"] == "created", result
    return result["order_id"]


def _days_ago(days: int, hour: int) -> datetime:
    day = local_today() - timedelta(days=days)
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=local_tz())


def test_verify_proves_analytics_is_derived_from_public():
    checks = jobs.verify()
    failed = [(name, detail) for name, ok, detail in checks if not ok]
    assert not failed, failed


def test_refresh_picks_up_a_late_offline_sale(client, admin_headers):
    # A sale made 20 days ago but only synced now must land on its own day — outside the
    # default 7-day window, so this only passes if refresh really detects what changed.
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_product(client, admin_headers, filial_id, qty=10)
    client_id = _make_client(client, admin_headers, filial_id)
    jobs.rebuild()

    occurred_at = _days_ago(20, 12)
    _push_sale(client, admin_headers, filial_id, client_id, product_id, 3, occurred_at)
    result = jobs.refresh(min_days=1)

    assert result.from_date <= occurred_at.date()
    with engine.connect() as conn:
        quantity = conn.execute(
            text("SELECT quantity FROM analytics.daily_sales WHERE product_id = :p AND sale_date = :d"),
            {"p": product_id, "d": occurred_at.date()},
        ).scalar()
    assert quantity == 3


def test_snapshot_is_dense_and_flags_stockout_days(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_product(client, admin_headers, filial_id, qty=3)
    client_id = _make_client(client, admin_headers, filial_id)
    order_id = _push_sale(client, admin_headers, filial_id, client_id, product_id, 3, _days_ago(2, 10))

    # The routes stamp stock movements with "now"; move them onto the timeline under test:
    # restocked 4 days ago, sold out 2 days ago — nothing on the days in between.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE stock_movements SET occurred_at = :ts WHERE product_id = :p AND reason = 'restock'"),
            {"ts": _days_ago(4, 9), "p": product_id},
        )
        conn.execute(
            text("""UPDATE stock_movements sm SET occurred_at = :ts FROM order_items oi
                    WHERE sm.order_item_id = oi.id AND oi.order_id = :o"""),
            {"ts": _days_ago(2, 10), "o": order_id},
        )
    jobs.rebuild()

    with engine.connect() as conn:
        rows = conn.execute(
            text("""SELECT snapshot_date, end_of_day_qty, was_out_of_stock FROM analytics.daily_stock_snapshot
                    WHERE product_id = :p ORDER BY snapshot_date"""),
            {"p": product_id},
        ).all()
    expected = [
        (local_today() - timedelta(days=4), 3, False),  # restocked
        (local_today() - timedelta(days=3), 3, False),  # no movement: carried forward
        (local_today() - timedelta(days=2), 0, True),   # sold out during the day
        (local_today() - timedelta(days=1), 0, True),   # the whole day at 0
    ]
    assert [tuple(r) for r in rows] == expected
