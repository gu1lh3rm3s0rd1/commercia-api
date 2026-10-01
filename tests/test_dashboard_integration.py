"""Integration tests for the Fase 6 dashboard module (indicators + reports) — require a
real Postgres with the schema applied and scripts/seed_admin.py already run. Skipped
automatically otherwise.

The dev DB accumulates data across sessions (no test-DB isolation), so assertions here
are delta-based (capture a baseline, then assert the expected change) rather than exact
totals, same approach already accepted for the known test_client_full_flow pagination
flake. Report queries additionally scope to start_date=today to keep each test's window
small regardless of how much history has piled up from earlier sessions.
"""

import random
import string
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from tests.conftest import requires_db

pytestmark = requires_db


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _unique_cpf() -> str:
    return "".join(random.choices(string.digits, k=11))


def _admin_filial_id(client, admin_headers) -> int:
    me = client.get("/api/v1/auth/me", headers=admin_headers).json()
    return me["accessible_filial_ids"][0]


def _today_utc_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


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
        json={"sku": _unique("SKU"), "name": "Produto dashboard", "category_id": category_id},
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
            "first_name": "Dashboard",
            "last_name": "Teste",
            "email": f"{_unique('c')}@example.com",
            "cpf": _unique_cpf(),
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _place_order(client, admin_headers, filial_id: int, client_id: int, product_id: int, qty: int, payment_method: str = "cash") -> dict:
    resp = client.post(
        f"/api/v1/orders?filial_id={filial_id}",
        json={
            "client_id": client_id,
            "payment_method": payment_method,
            "items": [{"product_id": product_id, "quantity": qty}],
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201
    return resp.json()


# ---------------------------------------------------------------------
# indicators
# ---------------------------------------------------------------------


def test_indicators_reflect_todays_activity(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    before = client.get(
        f"/api/v1/dashboard/indicators?filial_id={filial_id}", headers=admin_headers
    ).json()

    product_id = _make_stocked_product(client, admin_headers, filial_id, "20.00", qty=10)
    client_id = _make_client(client, admin_headers, filial_id)
    order = _place_order(client, admin_headers, filial_id, client_id, product_id, qty=2)
    order_total = Decimal(str(order["total"]))

    after = client.get(
        f"/api/v1/dashboard/indicators?filial_id={filial_id}", headers=admin_headers
    ).json()

    assert after["sales_today"]["count"] == before["sales_today"]["count"] + 1
    assert Decimal(str(after["sales_today"]["total"])) == Decimal(
        str(before["sales_today"]["total"])
    ) + order_total
    assert after["new_clients_today"] == before["new_clients_today"] + 1
    assert after["active_products"] == before["active_products"] + 1

    assert len(after["last_7_days"]) == 7
    today_str = _today_utc_iso()
    today_before = next(p for p in before["last_7_days"] if p["date"] == today_str)
    today_after = next(p for p in after["last_7_days"] if p["date"] == today_str)
    assert today_after["count"] == today_before["count"] + 1


def test_cancelled_orders_excluded_from_sales_today(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "15.00", qty=5)
    client_id = _make_client(client, admin_headers, filial_id)

    before = client.get(
        f"/api/v1/dashboard/indicators?filial_id={filial_id}", headers=admin_headers
    ).json()

    order = _place_order(client, admin_headers, filial_id, client_id, product_id, qty=1)
    client.post(f"/api/v1/orders/{order['id']}/cancel", headers=admin_headers)

    after = client.get(
        f"/api/v1/dashboard/indicators?filial_id={filial_id}", headers=admin_headers
    ).json()

    assert after["sales_today"]["count"] == before["sales_today"]["count"]
    assert Decimal(str(after["sales_today"]["total"])) == Decimal(str(before["sales_today"]["total"]))


# ---------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------


def test_sales_summary_totals_and_payment_breakdown(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=20)
    client_id = _make_client(client, admin_headers, filial_id)
    today = _today_utc_iso()

    before = client.get(
        f"/api/v1/dashboard/reports/sales-summary?filial_id={filial_id}&start_date={today}",
        headers=admin_headers,
    ).json()

    cash_order = _place_order(client, admin_headers, filial_id, client_id, product_id, qty=2, payment_method="cash")
    pix_order = _place_order(client, admin_headers, filial_id, client_id, product_id, qty=3, payment_method="pix")

    after = client.get(
        f"/api/v1/dashboard/reports/sales-summary?filial_id={filial_id}&start_date={today}",
        headers=admin_headers,
    ).json()

    assert after["total_orders"] == before["total_orders"] + 2
    expected_revenue = (
        Decimal(str(before["total_revenue"])) + Decimal(str(cash_order["total"])) + Decimal(str(pix_order["total"]))
    )
    assert Decimal(str(after["total_revenue"])) == expected_revenue

    def _find(breakdown, method):
        return next((b for b in breakdown if b["payment_method"] == method), {"count": 0, "total": "0"})

    cash_before, cash_after = _find(before["by_payment_method"], "cash"), _find(after["by_payment_method"], "cash")
    assert cash_after["count"] == cash_before["count"] + 1


def test_sales_summary_period_filter_excludes_out_of_range(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=5)
    client_id = _make_client(client, admin_headers, filial_id)

    _place_order(client, admin_headers, filial_id, client_id, product_id, qty=1)

    future_date = date(date.today().year + 1, 1, 1).isoformat()
    resp = client.get(
        f"/api/v1/dashboard/reports/sales-summary?filial_id={filial_id}&start_date={future_date}",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_orders"] == 0
    assert Decimal(str(body["total_revenue"])) == Decimal("0")


def test_sales_by_client_groups_and_orders_by_revenue_desc(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "50.00", qty=20)
    big_client_id = _make_client(client, admin_headers, filial_id)
    small_client_id = _make_client(client, admin_headers, filial_id)
    today = _today_utc_iso()

    _place_order(client, admin_headers, filial_id, big_client_id, product_id, qty=4)  # 200.00
    _place_order(client, admin_headers, filial_id, small_client_id, product_id, qty=1)  # 50.00

    resp = client.get(
        f"/api/v1/dashboard/reports/sales-by-client?filial_id={filial_id}&start_date={today}&limit=200",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    items = resp.json()["items"]

    big_index = next(i for i, it in enumerate(items) if it["client_id"] == big_client_id)
    small_index = next(i for i, it in enumerate(items) if it["client_id"] == small_client_id)
    assert big_index < small_index

    big_item = items[big_index]
    assert big_item["total_orders"] == 1
    assert Decimal(str(big_item["total_revenue"])) == Decimal("200.00")


def test_sales_by_status_counts_each_status(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=10)
    client_id = _make_client(client, admin_headers, filial_id)
    today = _today_utc_iso()

    before = client.get(
        f"/api/v1/dashboard/reports/sales-by-status?filial_id={filial_id}&start_date={today}",
        headers=admin_headers,
    ).json()
    before_counts = {item["status"]: item["count"] for item in before["items"]}

    _place_order(client, admin_headers, filial_id, client_id, product_id, qty=1)  # stays pending

    paid_order = _place_order(client, admin_headers, filial_id, client_id, product_id, qty=1)
    client.post(f"/api/v1/orders/{paid_order['id']}/pay", headers=admin_headers)

    cancelled_order = _place_order(client, admin_headers, filial_id, client_id, product_id, qty=1)
    client.post(f"/api/v1/orders/{cancelled_order['id']}/cancel", headers=admin_headers)

    after = client.get(
        f"/api/v1/dashboard/reports/sales-by-status?filial_id={filial_id}&start_date={today}",
        headers=admin_headers,
    ).json()
    after_counts = {item["status"]: item["count"] for item in after["items"]}

    assert after_counts.get("pending", 0) == before_counts.get("pending", 0) + 1
    assert after_counts.get("paid", 0) == before_counts.get("paid", 0) + 1
    assert after_counts.get("cancelled", 0) == before_counts.get("cancelled", 0) + 1


# ---------------------------------------------------------------------
# sales-by-category / top-products / sales-comparison / sales-by-hour-weekday
# ---------------------------------------------------------------------


def test_sales_by_category_groups_by_category(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
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
        json={"sku": _unique("SKU"), "name": "Produto categoria", "category_id": category_id},
        headers=admin_headers,
    ).json()["id"]
    client.post(
        f"/api/v1/catalog/products/{product_id}/prices", json={"price_default": "10.00"}, headers=admin_headers
    )
    client.post(
        f"/api/v1/catalog/products/{product_id}/stock/adjustments",
        json={"quantity_delta": 10, "reason": "restock"},
        headers=admin_headers,
    )
    client_id = _make_client(client, admin_headers, filial_id)
    today = _today_utc_iso()

    _place_order(client, admin_headers, filial_id, client_id, product_id, qty=3)  # 30.00

    resp = client.get(
        f"/api/v1/dashboard/reports/sales-by-category?filial_id={filial_id}&start_date={today}&limit=200",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    match = next(i for i in resp.json()["items"] if i["category_id"] == category_id)
    assert match["department_id"] == department_id
    assert match["quantity_sold"] == 3
    assert Decimal(str(match["total_revenue"])) == Decimal("30.00")


def test_top_products_sorts_by_requested_metric(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    cheap_high_qty = _make_stocked_product(client, admin_headers, filial_id, "5.00", qty=50)
    expensive_low_qty = _make_stocked_product(client, admin_headers, filial_id, "100.00", qty=10)
    client_id = _make_client(client, admin_headers, filial_id)
    today = _today_utc_iso()

    _place_order(client, admin_headers, filial_id, client_id, cheap_high_qty, qty=20)  # revenue 100, qty 20
    _place_order(client, admin_headers, filial_id, client_id, expensive_low_qty, qty=1)  # revenue 100, qty 1

    by_qty = client.get(
        f"/api/v1/dashboard/reports/top-products?filial_id={filial_id}&start_date={today}&sort_by=quantity&limit=200",
        headers=admin_headers,
    ).json()["items"]
    qty_rank = {it["product_id"]: i for i, it in enumerate(by_qty)}
    assert qty_rank[cheap_high_qty] < qty_rank[expensive_low_qty]


def test_sales_comparison_derives_previous_period_and_delta(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=10)
    client_id = _make_client(client, admin_headers, filial_id)
    today = date.today()
    url = (
        f"/api/v1/dashboard/reports/sales-comparison?filial_id={filial_id}"
        f"&start_date={today.isoformat()}&end_date={today.isoformat()}"
    )

    before = client.get(url, headers=admin_headers).json()
    assert before["previous"]["period_start"] == (today - timedelta(days=1)).isoformat()
    assert before["previous"]["period_end"] == (today - timedelta(days=1)).isoformat()

    _place_order(client, admin_headers, filial_id, client_id, product_id, qty=2)

    after = client.get(url, headers=admin_headers).json()
    assert after["current"]["total_orders"] == before["current"]["total_orders"] + 1


def test_sales_by_hour_and_weekday_reflect_new_order(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=5)
    client_id = _make_client(client, admin_headers, filial_id)
    today = _today_utc_iso()

    before_hour = client.get(
        f"/api/v1/dashboard/reports/sales-by-hour?filial_id={filial_id}&start_date={today}&end_date={today}",
        headers=admin_headers,
    ).json()
    before_weekday = client.get(
        f"/api/v1/dashboard/reports/sales-by-weekday?filial_id={filial_id}&start_date={today}&end_date={today}",
        headers=admin_headers,
    ).json()

    now = datetime.now(timezone.utc)
    _place_order(client, admin_headers, filial_id, client_id, product_id, qty=1)

    after_hour = client.get(
        f"/api/v1/dashboard/reports/sales-by-hour?filial_id={filial_id}&start_date={today}&end_date={today}",
        headers=admin_headers,
    ).json()
    after_weekday = client.get(
        f"/api/v1/dashboard/reports/sales-by-weekday?filial_id={filial_id}&start_date={today}&end_date={today}",
        headers=admin_headers,
    ).json()

    assert len(after_hour) == 24
    assert len(after_weekday) == 7
    assert after_hour[now.hour]["count"] == before_hour[now.hour]["count"] + 1
    assert after_weekday[now.weekday()]["count"] == before_weekday[now.weekday()]["count"] + 1


def test_sales_by_seller_ranks_by_revenue(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=10)
    client_id = _make_client(client, admin_headers, filial_id)
    today = _today_utc_iso()
    me = client.get("/api/v1/auth/me", headers=admin_headers).json()

    before = client.get(
        f"/api/v1/dashboard/reports/sales-by-seller?filial_id={filial_id}&start_date={today}&limit=200",
        headers=admin_headers,
    ).json()
    before_item = next((it for it in before["items"] if it["user_id"] == me["id"]), {"total_orders": 0})

    _place_order(client, admin_headers, filial_id, client_id, product_id, qty=2)

    after = client.get(
        f"/api/v1/dashboard/reports/sales-by-seller?filial_id={filial_id}&start_date={today}&limit=200",
        headers=admin_headers,
    ).json()
    after_item = next(it for it in after["items"] if it["user_id"] == me["id"])

    assert after_item["username"] == me["username"]
    assert after_item["total_orders"] == before_item["total_orders"] + 1


def test_sales_by_filial_lists_current_filial(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=10)
    client_id = _make_client(client, admin_headers, filial_id)
    today = _today_utc_iso()

    before = client.get(
        f"/api/v1/dashboard/reports/sales-by-filial?start_date={today}&limit=200", headers=admin_headers
    ).json()
    before_item = next((it for it in before["items"] if it["filial_id"] == filial_id), {"total_orders": 0})

    _place_order(client, admin_headers, filial_id, client_id, product_id, qty=1)

    after = client.get(
        f"/api/v1/dashboard/reports/sales-by-filial?start_date={today}&limit=200", headers=admin_headers
    ).json()
    after_item = next(it for it in after["items"] if it["filial_id"] == filial_id)

    assert after_item["total_orders"] == before_item["total_orders"] + 1


# ---------------------------------------------------------------------
# margin
# ---------------------------------------------------------------------


def test_margin_summary_and_by_product_use_frozen_cost_at_sale(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "20.00", qty=10)
    client.put(
        f"/api/v1/catalog/products/{product_id}/cost", json={"cost_value": "8.00"}, headers=admin_headers
    )
    client_id = _make_client(client, admin_headers, filial_id)
    today = _today_utc_iso()

    before_summary = client.get(
        f"/api/v1/dashboard/reports/margin-summary?filial_id={filial_id}&start_date={today}",
        headers=admin_headers,
    ).json()

    _place_order(client, admin_headers, filial_id, client_id, product_id, qty=3)  # revenue 60, cost 24, margin 36

    after_summary = client.get(
        f"/api/v1/dashboard/reports/margin-summary?filial_id={filial_id}&start_date={today}",
        headers=admin_headers,
    ).json()

    assert Decimal(str(after_summary["total_revenue"])) == Decimal(str(before_summary["total_revenue"])) + Decimal(
        "60.00"
    )
    assert Decimal(str(after_summary["total_cost"])) == Decimal(str(before_summary["total_cost"])) + Decimal("24.00")
    assert Decimal(str(after_summary["gross_margin"])) == Decimal(str(before_summary["gross_margin"])) + Decimal(
        "36.00"
    )

    by_product = client.get(
        f"/api/v1/dashboard/reports/margin-by-product?filial_id={filial_id}&start_date={today}&limit=200",
        headers=admin_headers,
    ).json()["items"]
    match = next(it for it in by_product if it["product_id"] == product_id)
    assert Decimal(str(match["revenue"])) == Decimal("60.00")
    assert Decimal(str(match["cost"])) == Decimal("24.00")
    assert Decimal(str(match["margin"])) == Decimal("36.00")


# ---------------------------------------------------------------------
# stock
# ---------------------------------------------------------------------


def test_low_stock_lists_products_at_or_below_threshold(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    low_product_id = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=2)
    high_product_id = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=100)

    resp = client.get(
        f"/api/v1/dashboard/reports/low-stock?filial_id={filial_id}&threshold=5&limit=200",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    product_ids = {it["product_id"] for it in resp.json()["items"]}
    assert low_product_id in product_ids
    assert high_product_id not in product_ids


def test_stale_products_includes_never_sold_excludes_recently_sold(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    never_sold_id = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=5)
    recently_sold_id = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=5)
    client_id = _make_client(client, admin_headers, filial_id)
    _place_order(client, admin_headers, filial_id, client_id, recently_sold_id, qty=1)

    resp = client.get(
        f"/api/v1/dashboard/reports/stale-products?filial_id={filial_id}&days=1&limit=200",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    product_ids = {it["product_id"] for it in items}
    assert recently_sold_id not in product_ids

    match = next(it for it in items if it["product_id"] == never_sold_id)
    assert match["last_sale_at"] is None
    assert match["days_since_last_sale"] is None


# ---------------------------------------------------------------------
# clients
# ---------------------------------------------------------------------


def test_new_clients_trend_zero_filled_and_counts_today(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)

    before = client.get(
        f"/api/v1/dashboard/reports/new-clients-trend?filial_id={filial_id}&days=7", headers=admin_headers
    ).json()
    assert len(before) == 7

    _make_client(client, admin_headers, filial_id)

    after = client.get(
        f"/api/v1/dashboard/reports/new-clients-trend?filial_id={filial_id}&days=7", headers=admin_headers
    ).json()
    today_str = _today_utc_iso()
    before_today = next(p for p in before if p["date"] == today_str)
    after_today = next(p for p in after if p["date"] == today_str)
    assert after_today["count"] == before_today["count"] + 1


def test_inactive_clients_includes_no_orders_excludes_recent_order(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _make_stocked_product(client, admin_headers, filial_id, "10.00", qty=5)
    never_ordered_id = _make_client(client, admin_headers, filial_id)
    recently_ordered_id = _make_client(client, admin_headers, filial_id)
    _place_order(client, admin_headers, filial_id, recently_ordered_id, product_id, qty=1)

    resp = client.get(
        f"/api/v1/dashboard/reports/inactive-clients?filial_id={filial_id}&days=1&limit=200",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    client_ids = {it["client_id"] for it in items}
    assert recently_ordered_id not in client_ids

    match = next(it for it in items if it["client_id"] == never_ordered_id)
    assert match["last_order_at"] is None
    assert match["days_since_last_order"] is None


def test_dashboard_endpoints_require_auth(client):
    endpoints = [
        "/api/v1/dashboard/indicators?filial_id=1",
        "/api/v1/dashboard/reports/sales-summary?filial_id=1",
        "/api/v1/dashboard/reports/sales-by-client?filial_id=1",
        "/api/v1/dashboard/reports/sales-by-status?filial_id=1",
        "/api/v1/dashboard/reports/sales-by-category?filial_id=1",
        "/api/v1/dashboard/reports/top-products?filial_id=1",
        "/api/v1/dashboard/reports/sales-comparison?filial_id=1&start_date=2026-01-01&end_date=2026-01-01",
        "/api/v1/dashboard/reports/sales-by-hour?filial_id=1",
        "/api/v1/dashboard/reports/sales-by-weekday?filial_id=1",
        "/api/v1/dashboard/reports/sales-by-seller?filial_id=1",
        "/api/v1/dashboard/reports/sales-by-filial",
        "/api/v1/dashboard/reports/margin-summary?filial_id=1",
        "/api/v1/dashboard/reports/margin-by-product?filial_id=1",
        "/api/v1/dashboard/reports/low-stock?filial_id=1",
        "/api/v1/dashboard/reports/stale-products?filial_id=1",
        "/api/v1/dashboard/reports/new-clients-trend?filial_id=1",
        "/api/v1/dashboard/reports/inactive-clients?filial_id=1",
    ]
    for url in endpoints:
        assert client.get(url).status_code == 401, url
