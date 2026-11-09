"""Integration tests for the panel's read routes (/analytics/* and /forecasts/compare|summary|backtest)
— require migrations up to 0008. They check the routes against the analytics/forecasting
tables directly, so the panel and (later) the agent can trust the numbers."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.analytics import jobs
from app.db.session import engine
from app.forecasting import evaluation, service
from tests.conftest import requires_db


def _schema_applied() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM forecasting.forecast_evaluations LIMIT 1"))
        return True
    except Exception:
        return False


pytestmark = [requires_db, pytest.mark.skipif(not _schema_applied(), reason="migrations up to 0008 not applied")]


@pytest.fixture(scope="module")
def ctx():
    jobs.rebuild()
    service.generate(models=["moving_average"])
    service.backtest(origins=1, models=["moving_average"])
    evaluation.evaluate()
    with engine.connect() as conn:
        filial_id = conn.execute(text("SELECT min(filial_id) FROM analytics.daily_sales")).scalar()
        category_id = conn.execute(
            text("""SELECT p.category_id FROM analytics.daily_sales s JOIN public.products p ON p.id = s.product_id
                    WHERE s.filial_id = :f GROUP BY 1 ORDER BY sum(s.revenue) DESC LIMIT 1"""),
            {"f": filial_id},
        ).scalar()
    return {"f": filial_id, "category_id": category_id}


def _get(client, headers, path, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    resp = client.get(f"/api/v1/{path}?{query}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _sales_total(filial_id, start, end) -> Decimal:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT coalesce(sum(revenue), 0) FROM analytics.daily_sales WHERE filial_id = :f AND sale_date BETWEEN :a AND :b"),
            {"f": filial_id, "a": start, "b": end},
        ).scalar()


def test_sales_daily_is_dense_and_matches_analytics(client, admin_headers, ctx):
    body = _get(client, admin_headers, "analytics/sales/daily", filial_id=ctx["f"])
    start, end = date.fromisoformat(body["start_date"]), date.fromisoformat(body["end_date"])
    assert (end - start).days + 1 == 28 == len(body["points"])  # default: 28 days, one point per day
    assert body["entity"]["level"] == "filial" and body["entity"]["name"]
    assert sum(Decimal(p["revenue"]) for p in body["points"]) == _sales_total(ctx["f"], start, end)
    assert all(Decimal(p["margin"]) == Decimal(p["revenue"]) - Decimal(p["cost"]) for p in body["points"])


def test_categories_add_up_to_the_filial(client, admin_headers, ctx):
    filial = _get(client, admin_headers, "analytics/sales/compare", filial_id=ctx["f"])
    ranking = _get(client, admin_headers, "analytics/sales/ranking", filial_id=ctx["f"], level="category", limit=100)
    assert sum(Decimal(i["revenue"]) for i in ranking["items"]) == Decimal(filial["current"]["revenue"])
    revenues = [Decimal(i["revenue"]) for i in ranking["items"]]
    assert revenues == sorted(revenues, reverse=True)
    assert abs(sum(Decimal(i["share_pct"]) for i in ranking["items"]) - 1) < Decimal("0.01")


def test_compare_defaults_to_the_previous_period_of_same_length(client, admin_headers, ctx):
    body = _get(client, admin_headers, "analytics/sales/compare", filial_id=ctx["f"], level="category",
                entity_id=ctx["category_id"], start_date="2026-09-01", end_date="2026-09-30")
    assert body["previous"]["start_date"] == "2026-08-02" and body["previous"]["end_date"] == "2026-08-31"
    current, previous = Decimal(body["current"]["revenue"]), Decimal(body["previous"]["revenue"])
    assert Decimal(body["change_pct"]["revenue"]) == ((current - previous) / previous).quantize(Decimal("0.0001"))


def test_stock_daily_and_stockouts_match_the_snapshot(client, admin_headers, ctx):
    outs = _get(client, admin_headers, "analytics/stockouts", filial_id=ctx["f"],
                start_date="2025-11-01", end_date="2026-10-31", limit=5)
    if not outs["items"]:
        pytest.skip("no stockout in this dataset")
    worst = outs["items"][0]
    stock = _get(client, admin_headers, "analytics/stock/daily", filial_id=ctx["f"], product_id=worst["product_id"],
                 start_date="2025-11-01", end_date="2026-10-31")
    assert sum(p["was_out_of_stock"] for p in stock["points"]) == worst["days_out"]
    assert all(p["end_of_day_qty"] >= 0 and p["min_qty"] <= p["end_of_day_qty"] for p in stock["points"])


def test_status_reports_freshness(client, admin_headers, ctx):
    body = _get(client, admin_headers, "analytics/status", filial_id=ctx["f"])
    assert body["last_job"]["status"] == "success"
    assert body["first_day"] <= body["last_sale_date"] <= body["last_day"]
    assert body["forecast_data_until"] is not None and body["last_evaluated_at"] is not None


def test_forecast_compare_joins_history_and_forecast(client, admin_headers, ctx):
    body = _get(client, admin_headers, "forecasts/compare", filial_id=ctx["f"], level="category",
                entity_id=ctx["category_id"], history_days=14)
    cut = date.fromisoformat(body["run"]["train_end"])
    assert len(body["history"]) == 14 and body["history"][-1]["date"] == cut.isoformat()
    assert body["forecast"][0]["date"] == (cut + timedelta(days=1)).isoformat()
    assert body["run"]["kind"] == "forecast" and body["entity"]["name"]


def test_summary_totals_are_the_sums_of_the_chart(client, admin_headers, ctx):
    summary = _get(client, admin_headers, "forecasts/summary", filial_id=ctx["f"], model="moving_average")
    chart = _get(client, admin_headers, "forecasts/compare", filial_id=ctx["f"], model="moving_average", history_days=14)
    q = summary["targets"]["quantity"]
    assert Decimal(q["next_7_days"]) == sum(Decimal(p["predicted"]) for p in chart["forecast"][:7])
    assert Decimal(q["last_7_days"]) == sum(Decimal(p["actual"]) for p in chart["history"][-7:])
    assert summary["data_until"] == chart["run"]["train_end"]
    assert q["expected_wape_days_1_7"] is not None  # backtest evaluated in the fixture


def test_backtest_series_compares_with_real_sales(client, admin_headers, ctx):
    body = _get(client, admin_headers, "forecasts/backtest", filial_id=ctx["f"], target="revenue")
    origin = body["origins"][0]
    first, last = origin["points"][0]["date"], origin["points"][-1]["date"]
    daily = _get(client, admin_headers, "analytics/sales/daily", filial_id=ctx["f"], start_date=first, end_date=last)
    assert [Decimal(p["actual"]) for p in origin["points"]] == [Decimal(p["revenue"]) for p in daily["points"]]
    assert origin["wape"] is not None


def test_errors_are_explicit(client, admin_headers, ctx):
    f = ctx["f"]
    assert client.get(f"/api/v1/analytics/sales/daily?filial_id={f}&level=product&entity_id=999999999",
                      headers=admin_headers).status_code == 404
    assert client.get(f"/api/v1/analytics/sales/daily?filial_id={f}&level=product", headers=admin_headers).status_code == 404
    assert client.get(f"/api/v1/analytics/sales/daily?filial_id={f}&start_date=2024-01-01&end_date=2026-01-01",
                      headers=admin_headers).status_code == 422
    assert client.get(f"/api/v1/analytics/sales/ranking?filial_id={f}&level=filial", headers=admin_headers).status_code == 422
    assert client.get(f"/api/v1/forecasts/summary?filial_id={f}&model=bola", headers=admin_headers).status_code == 422
    assert client.get(f"/api/v1/analytics/status?filial_id={f}").status_code == 401
