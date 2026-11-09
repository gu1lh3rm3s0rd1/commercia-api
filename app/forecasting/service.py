"""Forecast generation (Fase 8): runs the baseline models over the analytics series and
PERSISTS every result — there's no other way to obtain a forecast. Readers (the API, the
CLI) only ever read forecasting.forecasts rows that a successful run wrote; nothing is
computed live without leaving a trace.

One run = one model x one target (quantity | revenue) x one filial. Models run per
product; category, department and filial forecasts are the SUM of their products'
(bottom-up) — the levels always add up, and the out-of-stock adjustment carries up.

Each run also gets holdout metrics (team decision 2026-11-03): the last 28 days of the
training window are hidden, forecast, and compared against what actually sold
(uncensored days only) — MAE, WAPE and bias, at product and filial level.
"""

import json
import math
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import text

from app.core.timezone import local_today
from app.db.session import engine
from app.forecasting import features
from app.forecasting.models import baseline

TARGETS = ("quantity", "revenue")
DEFAULT_HORIZON = 14
BAND_Z = 1.2816  # 80% band
RESIDUAL_DAYS = 56  # one-step-ahead errors over the last 8 weeks give the band width
HOLDOUT_DAYS = 28


@dataclass
class RunSummary:
    run_id: int
    filial_id: int
    model: str
    target: str
    train_start: date
    train_end: date
    n_series: int
    metrics: dict
    kind: str = "forecast"


def _sigma(fn, params, values, dates) -> float:
    """Std of one-step-ahead errors over the last RESIDUAL_DAYS uncensored days."""
    errors = []
    for t in range(max(1, len(values) - RESIDUAL_DAYS), len(values)):
        if values[t] is not None:
            errors.append(values[t] - fn(values[:t], dates[:t], 1, **params)[0])
    return math.sqrt(sum(e * e for e in errors) / len(errors)) if errors else 0.0


def _holdout_metrics(fn, params, series: features.FilialSeries, target: str) -> dict:
    """Hide the last HOLDOUT_DAYS, forecast them, compare with what really sold."""
    dates = series.dates
    if len(dates) <= HOLDOUT_DAYS * 2:
        return {}
    cut = len(dates) - HOLDOUT_DAYS
    abs_err = err = actual_sum = 0.0
    points = 0
    filial_pred = [0.0] * HOLDOUT_DAYS
    filial_actual = [0.0] * HOLDOUT_DAYS
    for product in series.products.values():
        values = getattr(product, target)
        if all(v is None for v in values[:cut]):
            continue
        predicted = fn(values[:cut], dates[:cut], HOLDOUT_DAYS, **params)
        for h, actual in enumerate(values[cut:]):
            if actual is None:
                continue
            e = actual - predicted[h]
            abs_err += abs(e)
            err += e
            actual_sum += actual
            points += 1
            filial_pred[h] += predicted[h]
            filial_actual[h] += actual
    if not points:
        return {}
    filial_abs = sum(abs(a - p) for a, p in zip(filial_actual, filial_pred))
    filial_total = sum(filial_actual)
    return {
        "holdout_days": HOLDOUT_DAYS,
        "product": {
            "mae": round(abs_err / points, 4),
            "wape": round(abs_err / actual_sum, 4) if actual_sum else None,
            "bias": round(-err / actual_sum, 4) if actual_sum else None,  # > 0: over-forecasting
        },
        "filial": {
            "mae": round(filial_abs / HOLDOUT_DAYS, 4),
            "wape": round(filial_abs / filial_total, 4) if filial_total else None,
            "bias": round(-sum(a - p for a, p in zip(filial_actual, filial_pred)) / filial_total, 4)
            if filial_total else None,
        },
    }


def _money(value: float) -> float:
    return round(value + 0.0, 2)


def _band_rows(run_id, level, entity_id, dates, predicted, sigma) -> list[dict]:
    rows = []
    for d, p in zip(dates, predicted):
        p = max(0.0, p)
        rows.append({
            "run_id": run_id, "level": level, "entity_id": entity_id, "target_date": d,
            "predicted": _money(p),
            "lower": _money(max(0.0, p - BAND_Z * sigma)),
            "upper": _money(p + BAND_Z * sigma),
        })
    return rows


def _run_one(series: features.FilialSeries, model: str, target: str, horizon: int, kind: str = "forecast") -> RunSummary:
    fn, params = baseline.MODELS[model]
    with engine.begin() as conn:
        run_id = conn.execute(
            text("""
                INSERT INTO forecasting.forecast_runs
                    (model, model_version, params, target, filial_id, train_start, train_end, horizon_days, status, kind)
                VALUES (:model, :version, CAST(:params AS jsonb), :target, :filial, :start, :end, :horizon, 'running', :kind)
                RETURNING id
            """),
            {"model": model, "version": baseline.VERSION, "params": json.dumps(params), "target": target,
             "filial": series.filial_id, "start": series.dates[0], "end": series.dates[-1], "horizon": horizon,
             "kind": kind},
        ).scalar()

    try:
        future = [series.dates[-1] + timedelta(days=h) for h in range(1, horizon + 1)]
        rows: list[dict] = []
        # level -> entity -> (summed predictions, summed variance)
        totals: dict[str, dict[int, tuple[list[float], float]]] = {"category": {}, "department": {}, "filial": {}}
        for product in series.products.values():
            values = getattr(product, target)
            predicted = [max(0.0, p) for p in fn(values, series.dates, horizon, **params)]
            sigma = _sigma(fn, params, values, series.dates)
            rows += _band_rows(run_id, "product", product.product_id, future, predicted, sigma)
            for level, entity in (("category", product.category_id), ("department", product.department_id),
                                  ("filial", series.filial_id)):
                summed, variance = totals[level].get(entity, ([0.0] * horizon, 0.0))
                totals[level][entity] = ([a + b for a, b in zip(summed, predicted)], variance + sigma * sigma)
        for level, entities in totals.items():
            for entity, (summed, variance) in entities.items():
                # products' errors assumed independent: variances add up
                rows += _band_rows(run_id, level, entity, future, summed, math.sqrt(variance))

        metrics = _holdout_metrics(fn, params, series, target)
        n_series = len(series.products) + sum(len(e) for e in totals.values())
        with engine.begin() as conn:
            if rows:
                conn.execute(
                    text("""INSERT INTO forecasting.forecasts (run_id, level, entity_id, target_date, predicted, lower, upper)
                            VALUES (:run_id, :level, :entity_id, :target_date, :predicted, :lower, :upper)"""),
                    rows,
                )
            conn.execute(
                text("""UPDATE forecasting.forecast_runs SET status = 'success', finished_at = now(),
                            n_series = :n, metrics = CAST(:metrics AS jsonb) WHERE id = :id"""),
                {"n": n_series, "metrics": json.dumps(metrics), "id": run_id},
            )
    except Exception as exc:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE forecasting.forecast_runs SET status = 'failed', finished_at = now(), error = :e WHERE id = :id"),
                {"e": str(exc)[:2000], "id": run_id},
            )
        raise
    return RunSummary(run_id, series.filial_id, model, target, series.dates[0], series.dates[-1], n_series, metrics, kind)


def generate(
    filial_ids: list[int] | None = None,
    models: list[str] | None = None,
    targets: list[str] | None = None,
    horizon: int = DEFAULT_HORIZON,
    train_end: date | None = None,
) -> list[RunSummary]:
    """Runs every requested model x target for every filial and persists the results.
    train_end defaults to the filial's last day with a sale (up to yesterday)."""
    models = models or list(baseline.MODELS)
    targets = targets or list(TARGETS)
    for model in models:
        if model not in baseline.MODELS:
            raise ValueError(f"unknown model '{model}' — options: {', '.join(baseline.MODELS)}")
    yesterday = local_today() - timedelta(days=1)

    with engine.connect() as conn:
        if filial_ids is None:
            filial_ids = list(conn.execute(text("SELECT DISTINCT filial_id FROM analytics.daily_sales ORDER BY 1")).scalars())
        loaded = []
        for filial_id in filial_ids:
            end = train_end or features.last_sale_date(conn, filial_id, yesterday)
            series = features.load_filial_series(conn, filial_id, end) if end else None
            if series is not None and series.products:
                loaded.append(series)

    return [_run_one(series, model, target, horizon) for series in loaded for target in targets for model in models]


BACKTEST_ORIGINS = 12
BACKTEST_STEP_DAYS = 7
MIN_TRAIN_DAYS = 56


def backtest(
    filial_ids: list[int] | None = None,
    models: list[str] | None = None,
    targets: list[str] | None = None,
    horizon: int = DEFAULT_HORIZON,
    origins: int = BACKTEST_ORIGINS,
    step_days: int = BACKTEST_STEP_DAYS,
) -> list[RunSummary]:
    """Retroactive forecasts (rolling origin) so Fase 9 can score models without waiting:
    for each of `origins` cut-off dates, `step_days` apart, train ONLY on data up to that
    date and forecast the next `horizon` days — days that already happened. The latest
    cut-off is `horizon` days before the last day with a sale, so every predicted day has
    a real outcome. Persisted like any run, with kind='backtest' (the API never serves
    them). Re-running replaces the filial's previous backtest runs: they're evaluation
    artifacts, and duplicates would only re-weight the averages."""
    models = models or list(baseline.MODELS)
    targets = targets or list(TARGETS)
    yesterday = local_today() - timedelta(days=1)

    plans: list[tuple[int, list[features.FilialSeries]]] = []
    with engine.connect() as conn:
        if filial_ids is None:
            filial_ids = list(conn.execute(text("SELECT DISTINCT filial_id FROM analytics.daily_sales ORDER BY 1")).scalars())
        for filial_id in filial_ids:
            last_sale = features.last_sale_date(conn, filial_id, yesterday)
            if last_sale is None:
                continue
            series_list = []
            for k in range(origins):
                cut = last_sale - timedelta(days=horizon + k * step_days)
                series = features.load_filial_series(conn, filial_id, cut, exclude_inactive=False)
                if series is not None and series.products and len(series.dates) >= MIN_TRAIN_DAYS:
                    series_list.append(series)
            plans.append((filial_id, series_list))

    summaries: list[RunSummary] = []
    for filial_id, series_list in plans:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM forecasting.forecast_runs WHERE filial_id = :f AND kind = 'backtest'"), {"f": filial_id}
            )
        for series in sorted(series_list, key=lambda s: s.dates[-1]):
            summaries += [_run_one(series, model, target, horizon, kind="backtest") for target in targets for model in models]
    return summaries
