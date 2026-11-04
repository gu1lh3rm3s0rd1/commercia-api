"""Read-only access to persisted forecasts. Nothing here computes a forecast: every value
returned was written by a successful forecasting run (app/forecasting/service.py)."""

from sqlalchemy import text
from sqlalchemy.orm import Session

_RUN_COLUMNS = """id, model, model_version, params, target, filial_id, train_start, train_end,
                  horizon_days, created_at, n_series, metrics"""


def list_runs(db: Session, filial_id: int, target: str | None, limit: int) -> list[dict]:
    rows = db.execute(
        text(f"""
            SELECT {_RUN_COLUMNS} FROM forecasting.forecast_runs
            WHERE filial_id = :f AND status = 'success' AND (CAST(:t AS varchar) IS NULL OR target = :t)
            ORDER BY created_at DESC, id DESC LIMIT :limit
        """),
        {"f": filial_id, "t": target, "limit": limit},
    ).mappings().all()
    return [dict(r) for r in rows]


def pick_run(db: Session, filial_id: int, target: str, model: str | None) -> dict | None:
    """The run to serve: the latest successful one of `model`, or — when no model is
    given — among each model's latest successful run, the one with the lowest holdout
    WAPE at filial level (runs without metrics last, then the newest)."""
    row = db.execute(
        text(f"""
            WITH latest AS (
                SELECT DISTINCT ON (model) {_RUN_COLUMNS} FROM forecasting.forecast_runs
                WHERE filial_id = :f AND target = :t AND status = 'success'
                  AND (CAST(:m AS varchar) IS NULL OR model = :m)
                ORDER BY model, created_at DESC, id DESC
            )
            SELECT * FROM latest
            ORDER BY (metrics -> 'filial' ->> 'wape')::numeric ASC NULLS LAST, created_at DESC, id DESC
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
