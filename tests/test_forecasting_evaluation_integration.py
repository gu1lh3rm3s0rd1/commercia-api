"""Integration tests for Fase 9 (backtest + evaluation + accuracy report) — require a real
Postgres with migrations up to 0008 applied. Skipped automatically otherwise.

The scoring tests insert their own hand-made runs (kind='backtest', so the API never
serves them) with known errors, and delete them afterwards."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.analytics import jobs
from app.db.session import engine
from app.forecasting import evaluation, service
from tests.conftest import requires_db


def _evaluation_schema_applied() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM forecasting.forecast_evaluations LIMIT 1"))
        return True
    except Exception:
        return False


pytestmark = [
    requires_db,
    pytest.mark.skipif(not _evaluation_schema_applied(), reason="evaluation schema not applied yet (migration 0008)"),
]


@pytest.fixture(scope="module")
def data():
    jobs.rebuild()
    with engine.connect() as conn:
        filial_id, data_end = conn.execute(
            text("SELECT filial_id, max(sale_date) FROM analytics.daily_sales GROUP BY filial_id ORDER BY 1 LIMIT 1")
        ).one()
    return {"filial_id": filial_id, "data_end": data_end}


def _make_run(conn, filial_id, train_end, horizon, rows) -> int:
    """A hand-made successful run with the given forecast rows [(level, entity, day, predicted)]."""
    run_id = conn.execute(
        text("""
            INSERT INTO forecasting.forecast_runs (model, model_version, params, target, filial_id, train_start,
                                                   train_end, horizon_days, status, kind)
            VALUES ('moving_average', 'test', CAST(:p AS jsonb), 'quantity', :f, :start, :end, :h, 'success', 'backtest')
            RETURNING id
        """),
        {"p": json.dumps({}), "f": filial_id, "start": train_end - timedelta(days=30), "end": train_end, "h": horizon},
    ).scalar()
    conn.execute(
        text("""INSERT INTO forecasting.forecasts (run_id, level, entity_id, target_date, predicted, lower, upper)
                VALUES (:r, :l, :e, :d, :p, 0, :p)"""),
        [{"r": run_id, "l": level, "e": entity, "d": day, "p": predicted} for level, entity, day, predicted in rows],
    )
    return run_id


def _evaluations(run_id) -> dict:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM forecasting.forecast_evaluations WHERE run_id = :r"), {"r": run_id}
        ).mappings().all()
    return {(r["level"], r["horizon_bucket"]): r for r in rows}


def _drop_run(run_id) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM forecasting.forecast_runs WHERE id = :r"), {"r": run_id})


def _latest_full_week_cut(conn, filial_id, data_end):
    """The latest train_end whose next 7 days all have sales. A dev DB can have holes
    (simulated history ends, then a few manual sales days later), so don't assume the
    week right before data_end is complete."""
    days = set(conn.execute(
        text("SELECT DISTINCT sale_date FROM analytics.daily_sales WHERE filial_id = :f AND sale_date <= :e"),
        {"f": filial_id, "e": data_end},
    ).scalars().all())
    cut = data_end - timedelta(days=10)
    while not all(cut + timedelta(days=i) in days for i in range(1, 8)):
        cut -= timedelta(days=1)
        assert cut > data_end - timedelta(days=400), "no 7 consecutive days with sales in the dev DB"
    return cut


def test_scoring_math_on_a_known_10_percent_over_forecast(data):
    filial_id = data["filial_id"]
    with engine.begin() as conn:
        cut = _latest_full_week_cut(conn, filial_id, data["data_end"])
        actual = dict(conn.execute(
            text("""SELECT sale_date, sum(quantity) FROM analytics.daily_sales
                    WHERE filial_id = :f AND sale_date BETWEEN :a AND :b GROUP BY 1"""),
            {"f": filial_id, "a": cut + timedelta(days=1), "b": cut + timedelta(days=7)},
        ).all())
        assert len(actual) == 7
        run_id = _make_run(conn, filial_id, cut, 7, [
            ("filial", filial_id, day, Decimal(qty) * Decimal("1.1")) for day, qty in actual.items()
        ])
    try:
        evaluation.evaluate()
        e = _evaluations(run_id)[("filial", "1-7")]
        assert e["n_points"] == 7
        assert e["actual_total"] == sum(actual.values())
        assert e["wape"] == Decimal("0.1000")
        assert e["mape"] == Decimal("0.1000")
        assert e["bias"] == Decimal("0.1000")  # positive: over-forecasting
    finally:
        _drop_run(run_id)


def test_days_after_the_last_sale_are_never_scored(data):
    # forecast 7 days from 3 days before the data ends: only 3 of them really happened
    filial_id, cut = data["filial_id"], data["data_end"] - timedelta(days=3)
    with engine.begin() as conn:
        run_id = _make_run(conn, filial_id, cut, 7, [
            ("filial", filial_id, cut + timedelta(days=h), Decimal("100")) for h in range(1, 8)
        ])
    try:
        evaluation.evaluate()
        assert _evaluations(run_id)[("filial", "1-7")]["n_points"] == 3
    finally:
        _drop_run(run_id)


def test_out_of_stock_days_are_skipped_at_product_level(data):
    with engine.begin() as conn:
        censored = conn.execute(
            text("""SELECT product_id, snapshot_date FROM analytics.daily_stock_snapshot
                    WHERE was_out_of_stock AND snapshot_date <= :end ORDER BY snapshot_date DESC LIMIT 1"""),
            {"end": data["data_end"] - timedelta(days=1)},
        ).one_or_none()
        if censored is None:
            pytest.skip("no out-of-stock day in this dataset")
        product_id, day = censored
        clean_day = conn.execute(
            text("""SELECT snapshot_date FROM analytics.daily_stock_snapshot
                    WHERE product_id = :p AND NOT was_out_of_stock AND snapshot_date BETWEEN :a AND :b LIMIT 1"""),
            {"p": product_id, "a": day - timedelta(days=6), "b": day - timedelta(days=1)},
        ).scalar()
        if clean_day is None:
            pytest.skip("no stocked day right before the stockout")
        cut = clean_day - timedelta(days=1)
        run_id = _make_run(conn, data["filial_id"], cut, (day - cut).days, [
            ("product", product_id, clean_day, Decimal("1")), ("product", product_id, day, Decimal("1")),
        ])
    try:
        evaluation.evaluate()
        assert sum(e["n_points"] for e in _evaluations(run_id).values()) == 1  # the stockout day didn't count
    finally:
        _drop_run(run_id)


@pytest.fixture(scope="module")
def backtest_runs(data):
    service.generate(models=["moving_average"], targets=["quantity"])  # a real forecast, newer cut-off
    runs = service.backtest(origins=2)
    evaluation.evaluate()
    return runs


def test_backtest_trains_only_on_the_past_and_predicts_days_that_happened(data, backtest_runs):
    assert len(backtest_runs) == 2 * 4 * 2  # origins x models x targets
    assert {r.kind for r in backtest_runs} == {"backtest"}
    cuts = sorted({r.train_end for r in backtest_runs})
    assert cuts == [data["data_end"] - timedelta(days=21), data["data_end"] - timedelta(days=14)]
    with engine.connect() as conn:
        last_target = conn.execute(
            text("SELECT max(target_date) FROM forecasting.forecasts WHERE run_id = ANY(:ids)"),
            {"ids": [r.run_id for r in backtest_runs]},
        ).scalar()
    assert last_target <= data["data_end"]
    for r in backtest_runs:  # every predicted day already happened: both weeks fully scored
        e = _evaluations(r.run_id)
        assert e[("filial", "1-7")]["n_points"] == 7 and e[("filial", "8-14")]["n_points"] == 7


def test_rerunning_backtest_replaces_instead_of_duplicating(data, backtest_runs):
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM forecasting.forecast_runs WHERE filial_id = :f AND kind = 'backtest'"),
            {"f": data["filial_id"]},
        ).scalar()
    assert count == len(backtest_runs)


def test_api_never_serves_a_backtest(client, admin_headers, data, backtest_runs):
    f = data["filial_id"]
    body = client.get(f"/api/v1/forecasts?filial_id={f}&level=filial&entity_id={f}", headers=admin_headers).json()
    assert body["run"]["kind"] == "forecast"
    listed = client.get(f"/api/v1/forecasts/runs?filial_id={f}", headers=admin_headers).json()
    assert {r["kind"] for r in listed} == {"forecast"}


def test_accuracy_report_answers_how_far_off_with_a_number(client, admin_headers, data, backtest_runs):
    f = data["filial_id"]
    resp = client.get(f"/api/v1/forecasts/accuracy?filial_id={f}&target=quantity&kind=backtest", headers=admin_headers)
    assert resp.status_code == 200
    rows = resp.json()
    first_week = [r for r in rows if r["horizon_bucket"] == "1-7"]
    assert {r["model"] for r in first_week} == {"moving_average", "weighted_moving_average", "exp_smoothing", "period_comparison"}
    assert all(r["level"] == "filial" and r["n_runs"] == 2 and 0 < float(r["wape"]) < 1 for r in first_week)
    assert client.get(f"/api/v1/forecasts/accuracy?filial_id={f}").status_code == 401


def test_backtest_does_not_peek_at_product_status_after_the_cut(client, admin_headers, data):
    # a product that sold before the cut but is deactivated today must still be forecast "as of" the cut
    cut = data["data_end"] - timedelta(days=14)
    with engine.connect() as conn:
        product_id = conn.execute(
            text("""SELECT s.product_id FROM analytics.daily_sales s JOIN public.products p ON p.id = s.product_id
                    WHERE s.sale_date BETWEEN :a AND :b AND p.status = 'active'
                    GROUP BY 1 ORDER BY sum(s.quantity) ASC LIMIT 1"""),
            {"a": cut - timedelta(days=55), "b": cut},
        ).scalar()
    assert client.delete(f"/api/v1/catalog/products/{product_id}", headers=admin_headers).status_code == 200

    runs = service.backtest(origins=1, models=["moving_average"], targets=["quantity"])
    real = service.generate(models=["moving_average"], targets=["quantity"])
    with engine.connect() as conn:
        def forecast_products(run_id):
            return set(conn.execute(
                text("SELECT entity_id FROM forecasting.forecasts WHERE run_id = :r AND level = 'product'"), {"r": run_id}
            ).scalars())
        assert product_id in forecast_products(runs[0].run_id)
        assert product_id not in forecast_products(real[0].run_id)


def test_default_model_is_the_best_in_the_fase_9_evaluation(client, admin_headers, data):
    # Generate all 4 models for real, backtest them and evaluate: without ?model=, the API
    # must serve the model with the lowest pooled WAPE (filial level, first week) — not the
    # one that happened to win its single-window holdout.
    service.generate(targets=["quantity"])
    service.backtest(origins=2, targets=["quantity"])
    evaluation.evaluate()
    rows = evaluation.accuracy_report(data["filial_id"], "quantity", "filial")
    best = min((r for r in rows if r["horizon_bucket"] == "1-7" and r["model_version"] != "test"),
               key=lambda r: r["wape"])

    f = data["filial_id"]
    for path in (f"forecasts?filial_id={f}&level=filial&entity_id={f}", f"forecasts/compare?filial_id={f}"):
        assert client.get(f"/api/v1/{path}", headers=admin_headers).json()["run"]["model"] == best["model"], path
    summary = client.get(f"/api/v1/forecasts/summary?filial_id={f}", headers=admin_headers).json()
    assert summary["targets"]["quantity"]["model"] == best["model"]
