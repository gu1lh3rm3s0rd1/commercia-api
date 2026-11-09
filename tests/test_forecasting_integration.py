"""Integration tests for Fase 8 forecast generation and the read-only API — require a real
Postgres with migrations up to 0007 applied and analytics populated (the tests rebuild it).
Skipped automatically otherwise."""

from collections import defaultdict
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.analytics import jobs
from app.db.session import engine
from app.forecasting import service
from tests.conftest import requires_db


def _forecasting_schema_applied() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT metrics FROM forecasting.forecast_runs LIMIT 1"))
        return True
    except Exception:
        return False


pytestmark = [
    requires_db,
    pytest.mark.skipif(not _forecasting_schema_applied(), reason="forecasting schema not applied yet (migrations 0006/0007)"),
]


@pytest.fixture(scope="module")
def runs():
    jobs.rebuild()
    summaries = service.generate(models=["moving_average", "period_comparison"])
    assert summaries, "no filial with analytics data to forecast"
    return summaries


def _count_runs() -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM forecasting.forecast_runs")).scalar()


def test_every_run_is_persisted_with_its_forecasts_and_metrics(runs):
    with engine.connect() as conn:
        for r in runs:
            run = conn.execute(
                text("SELECT status, model_version, params, horizon_days, metrics FROM forecasting.forecast_runs WHERE id = :id"),
                {"id": r.run_id},
            ).one()
            assert run.status == "success"
            assert run.model_version and run.params is not None
            assert run.metrics["holdout_days"] == 28
            assert run.metrics["filial"]["wape"] is not None
            levels = dict(conn.execute(
                text("SELECT level, count(DISTINCT entity_id) FROM forecasting.forecasts WHERE run_id = :id GROUP BY level"),
                {"id": r.run_id},
            ).all())
            assert set(levels) == {"product", "category", "department", "filial"}
            assert levels["filial"] == 1
            days = conn.execute(
                text("SELECT count(DISTINCT target_date) FROM forecasting.forecasts WHERE run_id = :id"), {"id": r.run_id}
            ).scalar()
            assert days == run.horizon_days


def test_levels_add_up_bottom_up(runs):
    run = runs[0]
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT level, entity_id, target_date, predicted FROM forecasting.forecasts WHERE run_id = :id"),
            {"id": run.run_id},
        ).all()
    by_level: dict[str, dict] = defaultdict(lambda: defaultdict(Decimal))
    for level, _entity, day, predicted in rows:
        by_level[level][day] += predicted
    n_products = len({e for level, e, _, _ in rows if level == "product"})
    tolerance = Decimal("0.01") * n_products  # each product row is rounded to cents on its own
    for day, total in by_level["filial"].items():
        for level in ("product", "category", "department"):
            assert abs(by_level[level][day] - total) <= tolerance, (level, day)


def test_training_window_ends_on_the_last_day_with_a_sale(runs):
    with engine.connect() as conn:
        last_sale = conn.execute(
            text("SELECT max(sale_date) FROM analytics.daily_sales WHERE filial_id = :f"), {"f": runs[0].filial_id}
        ).scalar()
    assert all(r.train_end == last_sale for r in runs)


def test_api_serves_persisted_forecast_without_computing_anything(client, admin_headers, runs):
    filial_id = runs[0].filial_id
    before = _count_runs()
    resp = client.get(
        f"/api/v1/forecasts?filial_id={filial_id}&level=filial&entity_id={filial_id}&target=quantity",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert _count_runs() == before  # reading never creates a run

    with engine.connect() as conn:
        stored = conn.execute(
            text("""SELECT target_date, predicted FROM forecasting.forecasts
                    WHERE run_id = :r AND level = 'filial' AND entity_id = :e ORDER BY target_date"""),
            {"r": body["run"]["id"], "e": filial_id},
        ).all()
    assert [(p["date"], Decimal(p["predicted"])) for p in body["points"]] == [(d.isoformat(), v) for d, v in stored]

    # without ?model=, the most accurate model is served: by the Fase 9 evaluation when the
    # model was evaluated, otherwise by the run's own holdout — computed here from the DB,
    # since runs of other models may exist besides this module's
    with engine.connect() as conn:
        latest = conn.execute(
            text("""
                SELECT DISTINCT ON (model) model, model_version, (metrics -> 'filial' ->> 'wape')::numeric AS holdout
                FROM forecasting.forecast_runs
                WHERE filial_id = :f AND target = 'quantity' AND status = 'success' AND kind = 'forecast'
                ORDER BY model, created_at DESC, id DESC
            """),
            {"f": filial_id},
        ).all()
        evaluated = dict(conn.execute(
            text("""
                SELECT r.model || '/' || r.model_version, sum(e.abs_error_total) / nullif(sum(e.actual_total), 0)
                FROM forecasting.forecast_evaluations e JOIN forecasting.forecast_runs r ON r.id = e.run_id
                WHERE r.filial_id = :f AND r.target = 'quantity' AND e.level = 'filial' AND e.horizon_bucket = '1-7'
                GROUP BY 1
            """),
            {"f": filial_id},
        ).all())
    scored = [m for m in latest if f"{m.model}/{m.model_version}" in evaluated]
    if scored:
        best_model = min(scored, key=lambda m: evaluated[f"{m.model}/{m.model_version}"]).model
    else:
        best_model = min(latest, key=lambda m: m.holdout).model
    assert body["run"]["model"] == best_model


def test_api_can_pick_a_model_and_lists_runs(client, admin_headers, runs):
    filial_id = runs[0].filial_id
    resp = client.get(
        f"/api/v1/forecasts?filial_id={filial_id}&level=filial&entity_id={filial_id}&target=revenue&model=moving_average",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["run"]["model"] == "moving_average"

    listed = client.get(f"/api/v1/forecasts/runs?filial_id={filial_id}", headers=admin_headers).json()
    assert {r.run_id for r in runs} <= {r["id"] for r in listed}


def test_api_errors_are_explicit(client, admin_headers, runs):
    filial_id = runs[0].filial_id
    base = f"/api/v1/forecasts?filial_id={filial_id}&target=quantity"
    assert client.get(f"{base}&level=product&entity_id=999999999", headers=admin_headers).status_code == 404
    assert client.get(f"{base}&level=filial&entity_id={filial_id}&model=bola_de_cristal", headers=admin_headers).status_code == 422
    assert client.get(f"{base}&level=galaxy&entity_id=1", headers=admin_headers).status_code == 422
    assert client.get(f"{base}&level=filial&entity_id={filial_id}").status_code == 401
