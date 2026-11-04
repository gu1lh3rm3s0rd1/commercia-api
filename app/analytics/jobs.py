"""Jobs that populate the `analytics` schema from `public` (Fase 7).

Invoked on demand / by cron through app/workers/cli.py — never a permanent process:

    python -m app.workers.cli analytics rebuild   # drop everything, recompute from scratch
    python -m app.workers.cli analytics refresh   # recompute only what changed since the last run
    python -m app.workers.cli analytics verify    # prove the tables are derivable (Fase 7 criterion)

Everything is computed set-based inside Postgres (INSERT ... SELECT), one transaction per
run: the affected date range is deleted and re-inserted atomically, so readers never see
a half-built table. Today is never included — it's an incomplete day; the last day
computed is always yesterday (business timezone, see app/core/timezone.py).
"""

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.core.config import get_settings
from app.core.timezone import local_day_bounds, local_today
from app.db.session import engine

# Serializes analytics jobs (two crons overlapping, or a manual run during the cron).
_LOCK = "SELECT pg_advisory_xact_lock(hashtext('analytics_jobs'))"

_EFFECTIVE = "coalesce(o.occurred_at, o.created_at)"  # same "when did the sale happen" rule as the dashboard


@dataclass
class RunResult:
    job: str
    from_date: date | None
    to_date: date | None
    rows_sales: int
    rows_stock: int


def _yesterday() -> date:
    return local_today() - timedelta(days=1)


def _params(from_date: date, to_date: date) -> dict:
    start_ts, _ = local_day_bounds(from_date)
    _, end_ts = local_day_bounds(to_date)
    return {
        "from_date": from_date,
        "to_date": to_date,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "tz": get_settings().app_timezone,
    }


def _data_start(conn: Connection) -> date | None:
    """First business-timezone day with any sale or stock movement — where a rebuild starts."""
    return conn.execute(
        text(f"""
            SELECT least(
                (SELECT min(({_EFFECTIVE} AT TIME ZONE :tz)::date) FROM public.orders o),
                (SELECT min((occurred_at AT TIME ZONE :tz)::date) FROM public.stock_movements)
            )
        """),
        {"tz": get_settings().app_timezone},
    ).scalar()


def _watermarks(conn: Connection) -> tuple[int, int]:
    return (
        conn.execute(text("SELECT coalesce(max(id), 0) FROM public.orders")).scalar(),
        conn.execute(text("SELECT coalesce(max(id), 0) FROM public.stock_movements")).scalar(),
    )


_DAILY_SALES_SQL = f"""
    INSERT INTO analytics.daily_sales (sale_date, filial_id, product_id, orders_count, quantity, revenue, cost)
    SELECT ({_EFFECTIVE} AT TIME ZONE :tz)::date, o.filial_id, oi.product_id,
           count(DISTINCT o.id), sum(oi.quantity), sum(oi.total), sum(oi.quantity * oi.cost_at_sale)
    FROM public.orders o
    JOIN public.order_items oi ON oi.order_id = o.id
    WHERE o.status <> 'cancelled'
      AND {_EFFECTIVE} >= :start_ts AND {_EFFECTIVE} < :end_ts
    GROUP BY 1, 2, 3
"""

# Dense snapshot: one row per product per day, from its first-ever stock movement (or
# from_date, whichever is later) up to to_date. The running stock is resulting_qty of the
# last movement of the day in (occurred_at, id) order, carried forward over days without
# movements; `opening` seeds it with the last quantity before from_date, so a partial
# (incremental) recompute continues exactly where the untouched days left off.
_DAILY_STOCK_SQL = """
    WITH mv AS (
        SELECT product_id, filial_id, id, occurred_at, resulting_qty,
               (occurred_at AT TIME ZONE :tz)::date AS d
        FROM public.stock_movements
        WHERE occurred_at < :end_ts
    ),
    first_day AS (
        SELECT product_id, filial_id, min(d) AS first_d FROM mv GROUP BY product_id, filial_id
    ),
    opening AS (
        SELECT DISTINCT ON (product_id, filial_id) product_id, filial_id, resulting_qty AS qty
        FROM mv WHERE occurred_at < :start_ts
        ORDER BY product_id, filial_id, occurred_at DESC, id DESC
    ),
    day_stats AS (
        SELECT product_id, filial_id, d,
               (array_agg(resulting_qty ORDER BY occurred_at DESC, id DESC))[1] AS end_qty,
               min(resulting_qty) AS min_mv_qty
        FROM mv WHERE occurred_at >= :start_ts
        GROUP BY product_id, filial_id, d
    ),
    grid AS (
        SELECT f.product_id, f.filial_id, g.d::date AS d
        FROM first_day f
        CROSS JOIN LATERAL generate_series(
            greatest(f.first_d, CAST(:from_date AS date)), CAST(:to_date AS date), interval '1 day'
        ) AS g(d)
    ),
    filled AS (
        -- grp increments on every day that has movements: days without movements share
        -- the grp of the last day that had one, which is how the quantity carries forward
        SELECT g.product_id, g.filial_id, g.d, ds.end_qty, ds.min_mv_qty,
               count(ds.end_qty) OVER (PARTITION BY g.product_id, g.filial_id ORDER BY g.d) AS grp
        FROM grid g
        LEFT JOIN day_stats ds ON ds.product_id = g.product_id AND ds.filial_id = g.filial_id AND ds.d = g.d
    ),
    carried AS (
        SELECT f.product_id, f.filial_id, f.d, f.min_mv_qty,
               coalesce(max(f.end_qty) OVER (PARTITION BY f.product_id, f.filial_id, f.grp), o.qty, 0) AS eod
        FROM filled f
        LEFT JOIN opening o ON o.product_id = f.product_id AND o.filial_id = f.filial_id
    )
    INSERT INTO analytics.daily_stock_snapshot
        (snapshot_date, filial_id, product_id, end_of_day_qty, min_qty, was_out_of_stock)
    -- out of stock = a movement that day left it at 0, or the whole day passed at 0. A day
    -- that merely STARTS at 0 and is restocked doesn't count (e.g. sold out last night,
    -- restocked before opening) — that day's sales weren't constrained.
    SELECT d, filial_id, product_id, eod, coalesce(min_mv_qty, eod), coalesce(min_mv_qty, eod) <= 0
    FROM carried
"""


def _recompute(conn: Connection, from_date: date, to_date: date) -> tuple[int, int]:
    p = _params(from_date, to_date)
    conn.execute(text("DELETE FROM analytics.daily_sales WHERE sale_date BETWEEN :from_date AND :to_date"), p)
    conn.execute(text("DELETE FROM analytics.daily_stock_snapshot WHERE snapshot_date BETWEEN :from_date AND :to_date"), p)
    rows_sales = conn.execute(text(_DAILY_SALES_SQL), p).rowcount
    rows_stock = conn.execute(text(_DAILY_STOCK_SQL), p).rowcount
    return rows_sales, rows_stock


def _changed_since_last_run(conn: Connection) -> tuple[bool, date | None]:
    """(has a previous successful run, earliest day touched since it). Changes are new or
    updated orders (late offline sync, pay/cancel) and new stock movements — found by id
    watermark, since stock_movements has no 'recorded at' column. A transaction still open
    while the last run read its watermark can commit a lower id afterwards; the minimum
    window refresh() always recomputes covers that case."""
    last = conn.execute(text("""
        SELECT started_at, last_order_id, last_stock_movement_id FROM analytics.job_runs
        WHERE status = 'success' ORDER BY id DESC LIMIT 1
    """)).one_or_none()
    if last is None:
        return False, None
    tz = get_settings().app_timezone
    earliest = conn.execute(
        text(f"""
            SELECT least(
                (SELECT min(({_EFFECTIVE} AT TIME ZONE :tz)::date) FROM public.orders o
                  WHERE o.id > :last_order OR o.updated_at > :since),
                (SELECT min((occurred_at AT TIME ZONE :tz)::date) FROM public.stock_movements
                  WHERE id > :last_movement)
            )
        """),
        {"tz": tz, "last_order": last.last_order_id, "since": last.started_at, "last_movement": last.last_stock_movement_id},
    ).scalar()
    return True, earliest


def _run(job: str, body) -> RunResult:
    with engine.begin() as conn:
        run_id = conn.execute(
            text("INSERT INTO analytics.job_runs (job, status) VALUES (:job, 'running') RETURNING id"), {"job": job}
        ).scalar()
    try:
        with engine.begin() as conn:
            conn.execute(text(_LOCK))
            last_order_id, last_movement_id = _watermarks(conn)
            result = body(conn)
            conn.execute(
                text("""
                    UPDATE analytics.job_runs SET status = 'success', finished_at = now(),
                        from_date = :from_date, to_date = :to_date, rows_sales = :rows_sales, rows_stock = :rows_stock,
                        last_order_id = :last_order_id, last_stock_movement_id = :last_movement_id
                    WHERE id = :id
                """),
                {**result.__dict__, "last_order_id": last_order_id, "last_movement_id": last_movement_id, "id": run_id},
            )
        return result
    except Exception as exc:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE analytics.job_runs SET status = 'failed', finished_at = now(), error = :error WHERE id = :id"),
                {"error": str(exc)[:2000], "id": run_id},
            )
        raise


def rebuild() -> RunResult:
    """Drops every analytics row and recomputes the whole history from `public`."""

    def body(conn: Connection) -> RunResult:
        conn.execute(text("TRUNCATE analytics.daily_sales, analytics.daily_stock_snapshot"))
        start, end = _data_start(conn), _yesterday()
        if start is None or start > end:
            return RunResult("rebuild", None, None, 0, 0)
        return RunResult("rebuild", start, end, *_recompute(conn, start, end))

    return _run("rebuild", body)


def refresh(min_days: int = 7) -> RunResult:
    """Recomputes from the earliest day that changed since the last successful run up to
    yesterday — never less than the last `min_days` days. Through yesterday, not just the
    changed days: the stock snapshot carries quantities forward, so changing day D changes
    every day after it. Without a previous successful run, falls back to a full rebuild."""

    def body(conn: Connection) -> RunResult:
        has_previous, earliest_changed = _changed_since_last_run(conn)
        if not has_previous:
            conn.execute(text("TRUNCATE analytics.daily_sales, analytics.daily_stock_snapshot"))
            earliest_changed = _data_start(conn)
        end = _yesterday()
        start = end - timedelta(days=min_days - 1)
        if earliest_changed is not None:
            start = min(start, earliest_changed)
        data_start = _data_start(conn)
        if data_start is None:
            return RunResult("refresh", None, None, 0, 0)
        start = max(start, data_start)
        if start > end:
            return RunResult("refresh", None, None, 0, 0)
        return RunResult("refresh", start, end, *_recompute(conn, start, end))

    return _run("refresh", body)


# ---------------------------------------------------------------------
# verify — Fase 7's completion criterion
# ---------------------------------------------------------------------


def tables_hash() -> str:
    """Fingerprint of both analytics tables' full contents, in primary-key order."""
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT md5(
                (SELECT coalesce(string_agg(t::text, '|' ORDER BY sale_date, filial_id, product_id), '')
                   FROM analytics.daily_sales t)
                || '#' ||
                (SELECT coalesce(string_agg(t::text, '|' ORDER BY snapshot_date, filial_id, product_id), '')
                   FROM analytics.daily_stock_snapshot t)
            )
        """)).scalar()


def verify() -> list[tuple[str, bool, str]]:
    """Proves the analytics tables are derived from `public`, not a second source of truth:
    rebuilding twice gives identical tables, an incremental refresh gives the same tables
    as a full rebuild, and the totals match `public` directly."""
    checks: list[tuple[str, bool, str]] = []

    rebuild()
    first = tables_hash()
    rebuild()
    second = tables_hash()
    checks.append(("rebuild twice -> identical tables", first == second, f"{first} / {second}"))

    refresh(min_days=30)
    refreshed = tables_hash()
    checks.append(("refresh (last 30 days) over a rebuild -> identical tables", refreshed == first, refreshed))

    yesterday = _yesterday()
    p = _params(yesterday, yesterday)
    today_start, _ = local_day_bounds(local_today())
    with engine.connect() as conn:
        public_totals = conn.execute(text(f"""
            SELECT coalesce(sum(oi.quantity), 0), coalesce(sum(oi.total), 0), coalesce(sum(oi.quantity * oi.cost_at_sale), 0)
            FROM public.orders o JOIN public.order_items oi ON oi.order_id = o.id
            WHERE o.status <> 'cancelled' AND {_EFFECTIVE} < :end_ts
        """), p).one()
        analytics_totals = conn.execute(text(
            "SELECT coalesce(sum(quantity), 0), coalesce(sum(revenue), 0), coalesce(sum(cost), 0) FROM analytics.daily_sales"
        )).one()
        checks.append((
            "daily_sales totals (quantity, revenue, cost) == public order items",
            tuple(public_totals) == tuple(analytics_totals),
            f"public={tuple(public_totals)} analytics={tuple(analytics_totals)}",
        ))

        # Yesterday's end-of-day stock must equal the current stock, for every product that
        # had no movement today (today's movements are past the snapshot by design).
        mismatches = conn.execute(text("""
            SELECT count(*) FROM public.stocks st
            LEFT JOIN analytics.daily_stock_snapshot s
                   ON s.product_id = st.product_id AND s.filial_id = st.filial_id AND s.snapshot_date = :day
            WHERE NOT EXISTS (SELECT 1 FROM public.stock_movements m
                              WHERE m.product_id = st.product_id AND m.occurred_at >= :today_start)
              AND EXISTS (SELECT 1 FROM public.stock_movements m WHERE m.product_id = st.product_id)
              AND (s.end_of_day_qty IS NULL OR s.end_of_day_qty <> st.quantity)
        """), {"day": yesterday, "today_start": today_start}).scalar()
        checks.append(("yesterday's end_of_day_qty == public.stocks.quantity", mismatches == 0, f"{mismatches} mismatches"))
    return checks
