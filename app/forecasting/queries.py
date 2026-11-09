"""Read queries over persisted forecasts — behind the /api/v1/forecasts routes and, later,
the AI agent's forecasting tools (Fase 11). Nothing here computes a forecast: every value
comes from a successful forecasting run (app/forecasting/service.py), so there's always a
trace of which model produced it.
"""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.analytics.queries import Entity, ratio, sales_daily
from app.forecasting import evaluation


class NoForecast(LookupError):
    pass


_RUN_COLUMNS = """id, kind, model, model_version, params, target, filial_id, train_start, train_end,
                  horizon_days, created_at, n_series, metrics"""


def list_runs(db: Session, filial_id: int, target: str | None, kind: str | None, limit: int) -> list[dict]:
    rows = db.execute(
        text(f"""
            SELECT {_RUN_COLUMNS} FROM forecasting.forecast_runs
            WHERE filial_id = :f AND status = 'success' AND (CAST(:t AS varchar) IS NULL OR target = :t)
              AND (CAST(:k AS varchar) IS NULL OR kind = :k)
            ORDER BY created_at DESC, id DESC LIMIT :limit
        """),
        {"f": filial_id, "t": target, "k": kind, "limit": limit},
    ).mappings().all()
    return [dict(r) for r in rows]


def pick_run(db: Session, filial_id: int, target: str, model: str | None) -> dict | None:
    """The run to serve: the latest successful one of `model`, or — when no model is
    given — among each model's latest successful run, the one whose model version is the
    most accurate. "Most accurate" is the Fase 9 evaluation (WAPE pooled over every
    evaluated run, filial level, first week ahead — 12 backtest cut-offs, so it's stable);
    only models never evaluated (e.g. a brand-new filial) fall back to the run's own
    single-window holdout WAPE (Fase 8), which is noisier. Only real forecasts
    (kind='forecast'): a backtest is newer but looks at the past."""
    row = db.execute(
        text(f"""
            WITH latest AS (
                SELECT DISTINCT ON (model) {_RUN_COLUMNS} FROM forecasting.forecast_runs
                WHERE filial_id = :f AND target = :t AND status = 'success' AND kind = 'forecast'
                  AND (CAST(:m AS varchar) IS NULL OR model = :m)
                ORDER BY model, created_at DESC, id DESC
            ),
            evaluated AS (
                SELECT r.model, r.model_version,
                       sum(e.abs_error_total) / nullif(sum(e.actual_total), 0) AS wape
                FROM forecasting.forecast_evaluations e
                JOIN forecasting.forecast_runs r ON r.id = e.run_id
                WHERE r.filial_id = :f AND r.target = :t AND e.level = 'filial' AND e.horizon_bucket = '1-7'
                GROUP BY r.model, r.model_version
            )
            SELECT latest.* FROM latest
            LEFT JOIN evaluated ON evaluated.model = latest.model AND evaluated.model_version = latest.model_version
            ORDER BY evaluated.wape ASC NULLS LAST,
                     (latest.metrics -> 'filial' ->> 'wape')::numeric ASC NULLS LAST,
                     latest.created_at DESC, latest.id DESC
            LIMIT 1
        """),
        {"f": filial_id, "t": target, "m": model},
    ).mappings().one_or_none()
    return dict(row) if row else None


def get_points(db: Session, run_id: int, level: str, entity_id: int) -> list[dict]:
    rows = db.execute(
        text("""
            SELECT target_date AS date, predicted, lower, upper FROM forecasting.forecasts
            WHERE run_id = :r AND level = :l AND entity_id = :e ORDER BY target_date
        """),
        {"r": run_id, "l": level, "e": entity_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def _run_and_points(db: Session, filial_id: int, entity: Entity, target: str, model: str | None) -> tuple[dict, list[dict]]:
    run = pick_run(db, filial_id, target, model)
    if run is None:
        raise NoForecast(f"no forecast generated yet for filial {filial_id} ({target})")
    points = get_points(db, run["id"], entity.level, entity.id)
    if not points:
        raise NoForecast(
            f"run {run['id']} has no forecast for {entity.level} {entity.id} (inactive, or no sales in the last 8 weeks)"
        )
    return run, points


def forecast_compare(
    db: Session, filial_id: int, entity: Entity, target: str, model: str | None = None, history_days: int = 28
) -> dict:
    """The panel's main chart in one call: the last `history_days` real days up to the
    forecast's training cut-off, then the forecast with its 80% band."""
    run, points = _run_and_points(db, filial_id, entity, target, model)
    end = run["train_end"]
    history = sales_daily(db, filial_id, entity, end - timedelta(days=history_days - 1), end)
    return {
        "entity": entity,
        "run": run,
        "history": [{"date": h["date"], "actual": h[target]} for h in history],
        "forecast": points,
    }


def forecast_summary(db: Session, filial_id: int, entity: Entity, model: str | None = None) -> dict:
    """Ready-made answer to "how much will I sell next week": per target, the next 7/14 days
    forecast vs the last 7/14 real days, the change, the model used and how far off that
    model usually is (Fase 9 accuracy at this level)."""
    targets = {}
    data_until = None
    for target in ("quantity", "revenue"):
        try:
            run, points = _run_and_points(db, filial_id, entity, target, model)
        except NoForecast:
            continue
        data_until = run["train_end"]
        history = sales_daily(db, filial_id, entity, data_until - timedelta(days=13), data_until)
        actual = [h[target] for h in history]
        predicted = [p["predicted"] for p in points]
        next_7, next_14 = sum(predicted[:7], Decimal(0)), sum(predicted[:14], Decimal(0))
        last_7, last_14 = sum(actual[-7:], Decimal(0)), sum(actual, Decimal(0))
        accuracy = {
            r["horizon_bucket"]: r["wape"]
            for r in evaluation.accuracy_report(filial_id, target, entity.level)
            if r["model"] == run["model"] and r["model_version"] == run["model_version"]
        }
        targets[target] = {
            "model": run["model"],
            "model_version": run["model_version"],
            "run_id": run["id"],
            "next_7_days": next_7,
            "next_14_days": next_14,
            "last_7_days": last_7,
            "last_14_days": last_14,
            "change_7_days_pct": ratio(next_7 - last_7, last_7),
            "change_14_days_pct": ratio(next_14 - last_14, last_14),
            "expected_wape_days_1_7": accuracy.get("1-7"),
            "expected_wape_days_8_14": accuracy.get("8-14"),
        }
    if not targets:
        raise NoForecast(f"no forecast for {entity.level} {entity.id} in filial {filial_id}")
    return {"entity": entity, "data_until": data_until, "targets": targets}


def backtest_series(db: Session, filial_id: int, entity: Entity, target: str, model: str | None = None) -> list[dict]:
    """Predicted vs. real for every retroactive cut-off (Fase 9) — where each model hit and
    missed. Out-of-stock days are flagged at product level (the real figure there was
    capped by stock), not dropped, so the chart stays continuous."""
    runs = db.execute(
        text("""
            SELECT id, model, model_version, train_end FROM forecasting.forecast_runs
            WHERE filial_id = :f AND target = :t AND status = 'success' AND kind = 'backtest'
              AND (CAST(:m AS varchar) IS NULL OR model = :m)
            ORDER BY model, train_end
        """),
        {"f": filial_id, "t": target, "m": model},
    ).mappings().all()
    if not runs:
        raise NoForecast(f"no backtest for filial {filial_id} ({target}) — run 'forecast backtest'")

    first = min(r["train_end"] for r in runs) + timedelta(days=1)
    last = max(r["train_end"] for r in runs) + timedelta(days=31)
    actual = {d["date"]: d[target] for d in sales_daily(db, filial_id, entity, first, last)}
    out_of_stock = set()
    if entity.level == "product":
        out_of_stock = set(db.execute(
            text("""SELECT snapshot_date FROM analytics.daily_stock_snapshot
                    WHERE product_id = :p AND was_out_of_stock AND snapshot_date BETWEEN :a AND :b"""),
            {"p": entity.id, "a": first, "b": last},
        ).scalars())

    result = []
    for run in runs:
        points = [
            {"date": p["date"], "predicted": p["predicted"], "actual": actual.get(p["date"], Decimal(0)),
             "out_of_stock": p["date"] in out_of_stock}
            for p in get_points(db, run["id"], entity.level, entity.id)
        ]
        if not points:
            continue
        scored = [p for p in points if not p["out_of_stock"]]
        abs_error = sum((abs(p["predicted"] - p["actual"]) for p in scored), Decimal(0))
        result.append({
            "run_id": run["id"], "model": run["model"], "model_version": run["model_version"],
            "train_end": run["train_end"], "wape": ratio(abs_error, sum((p["actual"] for p in scored), Decimal(0))),
            "points": points,
        })
    return result
