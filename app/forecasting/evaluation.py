"""Forecast evaluation (Fase 9): scores every persisted forecast against what really sold.

    python -m app.workers.cli forecast evaluate

Crosses forecasting.forecasts with analytics.daily_sales on the days that already
happened — up to each filial's last day with a sale, never beyond: after it there's no
data (e.g. the simulated history ended), and comparing against 0 would invent a 100%
error. Fully recomputed on every run (the evaluations are derived), one transaction.

Per run x level x horizon bucket (days ahead: '1-7', '8-14', '15+'):
- WAPE = total |error| / total sold — the headline "erra em média X%". Weighted by volume,
  so a day selling 1 unit can't dominate.
- MAPE = mean of the per-day |error| / sold — only at aggregated levels and only on days
  with sales: at product level most days sell 0 or 1, where it's undefined or explodes.
- MAE = mean |error| in units (or R$); bias = (predicted - sold) / sold, > 0 = over-forecast.
Product level skips out-of-stock days (what sold was capped by the stock, the model
forecasts demand); aggregated levels compare against total real sales — what the
business sees.
"""

from sqlalchemy import text

from app.db.session import engine

_EVALUATE_SQL = """
    WITH data_end AS (
        SELECT filial_id, max(sale_date) AS last_day FROM analytics.daily_sales GROUP BY filial_id
    ),
    fc AS (
        SELECT f.run_id, f.level, f.entity_id, f.target_date, f.predicted, r.target,
               f.target_date - r.train_end AS days_ahead
        FROM forecasting.forecasts f
        JOIN forecasting.forecast_runs r ON r.id = f.run_id AND r.status = 'success'
        JOIN data_end de ON de.filial_id = r.filial_id
        WHERE f.target_date <= de.last_day
    ),
    product_map AS (
        SELECT p.id AS product_id, p.category_id, c.department_id
        FROM public.products p JOIN public.categories c ON c.id = p.category_id
    ),
    actuals AS (
        SELECT 'product' AS level, s.product_id AS entity_id, s.sale_date,
               s.quantity::numeric AS quantity, s.revenue
        FROM analytics.daily_sales s
        UNION ALL
        SELECT 'category', m.category_id, s.sale_date, sum(s.quantity), sum(s.revenue)
        FROM analytics.daily_sales s JOIN product_map m ON m.product_id = s.product_id
        GROUP BY m.category_id, s.sale_date
        UNION ALL
        SELECT 'department', m.department_id, s.sale_date, sum(s.quantity), sum(s.revenue)
        FROM analytics.daily_sales s JOIN product_map m ON m.product_id = s.product_id
        GROUP BY m.department_id, s.sale_date
        UNION ALL
        SELECT 'filial', s.filial_id, s.sale_date, sum(s.quantity), sum(s.revenue)
        FROM analytics.daily_sales s
        GROUP BY s.filial_id, s.sale_date
    ),
    points AS (
        SELECT fc.run_id, fc.level, fc.days_ahead, fc.predicted,
               coalesce(CASE WHEN fc.target = 'quantity' THEN a.quantity ELSE a.revenue END, 0) AS actual
        FROM fc
        LEFT JOIN actuals a ON a.level = fc.level AND a.entity_id = fc.entity_id AND a.sale_date = fc.target_date
        LEFT JOIN analytics.daily_stock_snapshot ss
               ON fc.level = 'product' AND ss.product_id = fc.entity_id AND ss.snapshot_date = fc.target_date
        WHERE NOT (fc.level = 'product' AND coalesce(ss.was_out_of_stock, false))
    )
    INSERT INTO forecasting.forecast_evaluations
        (run_id, level, horizon_bucket, n_points, actual_total, abs_error_total, mae, wape, mape, bias)
    SELECT run_id, level,
           CASE WHEN days_ahead <= 7 THEN '1-7' WHEN days_ahead <= 14 THEN '8-14' ELSE '15+' END AS bucket,
           count(*),
           sum(actual),
           sum(abs(predicted - actual)),
           sum(abs(predicted - actual)) / count(*),
           sum(abs(predicted - actual)) / nullif(sum(actual), 0),
           CASE WHEN level <> 'product'
                THEN avg(abs(predicted - actual) / actual) FILTER (WHERE actual > 0) END,
           sum(predicted - actual) / nullif(sum(actual), 0)
    FROM points
    GROUP BY run_id, level, bucket
"""


def evaluate() -> int:
    """Recomputes every evaluation; returns how many runs got at least one scored day."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM forecasting.forecast_evaluations"))
        conn.execute(text(_EVALUATE_SQL))
        return conn.execute(text("SELECT count(DISTINCT run_id) FROM forecasting.forecast_evaluations")).scalar()


def accuracy_report(
    filial_id: int, target: str | None = None, level: str | None = None, kind: str | None = None
) -> list[dict]:
    """Accuracy per model x version x target x level x horizon bucket, pooled over every
    evaluated run: WAPE and bias are volume-weighted (total error / total sold across all
    runs), MAPE is the mean of the runs' MAPEs."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT r.model, r.model_version, r.target, e.level, e.horizon_bucket,
                       count(DISTINCT r.id) AS n_runs,
                       sum(e.n_points) AS n_points,
                       round(sum(e.abs_error_total) / nullif(sum(e.actual_total), 0), 4) AS wape,
                       round(avg(e.mape), 4) AS mape,
                       round(sum(e.abs_error_total) / sum(e.n_points), 4) AS mae,
                       round(sum(e.bias * e.actual_total) / nullif(sum(e.actual_total), 0), 4) AS bias,
                       min(r.train_end) AS first_train_end,
                       max(r.train_end) AS last_train_end
                FROM forecasting.forecast_evaluations e
                JOIN forecasting.forecast_runs r ON r.id = e.run_id
                WHERE r.filial_id = :f
                  AND (CAST(:t AS varchar) IS NULL OR r.target = :t)
                  AND (CAST(:l AS varchar) IS NULL OR e.level = :l)
                  AND (CAST(:k AS varchar) IS NULL OR r.kind = :k)
                GROUP BY r.model, r.model_version, r.target, e.level, e.horizon_bucket
                ORDER BY r.target, e.level, e.horizon_bucket, wape NULLS LAST, r.model
            """),
            {"f": filial_id, "t": target, "l": level, "k": kind},
        ).mappings().all()
    return [dict(r) for r in rows]


def evaluated_filial_ids() -> list[int]:
    with engine.connect() as conn:
        return list(conn.execute(text("""
            SELECT DISTINCT r.filial_id FROM forecasting.forecast_evaluations e
            JOIN forecasting.forecast_runs r ON r.id = e.run_id ORDER BY 1
        """)).scalars())
