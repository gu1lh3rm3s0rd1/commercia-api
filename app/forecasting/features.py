"""Builds the daily series the forecasting models consume — from `analytics` only, never
from the operational tables directly (Fase 7/8 split).

Per product: a dense day-by-day series (daily_stock_snapshot has every day, daily_sales
only the days with a sale, so a missing sale is a 0) in which out-of-stock days are None
— censored: what sold was capped by the stock, not by demand. Days before the product
existed are None too.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

RECENT_SALES_DAYS = 56  # a product with no sale in the last 8 weeks isn't forecast


@dataclass
class ProductSeries:
    product_id: int
    category_id: int
    department_id: int
    quantity: list[float | None] = field(default_factory=list)
    revenue: list[float | None] = field(default_factory=list)


@dataclass
class FilialSeries:
    filial_id: int
    dates: list[date]
    products: dict[int, ProductSeries]


def last_sale_date(conn: Connection, filial_id: int, up_to: date) -> date | None:
    """Default end of the training window: the last day with any sale. Days after it are
    "no data" (e.g. the simulated history ends), not "zero demand"."""
    return conn.execute(
        text("SELECT max(sale_date) FROM analytics.daily_sales WHERE filial_id = :f AND sale_date <= :d"),
        {"f": filial_id, "d": up_to},
    ).scalar()


def load_filial_series(
    conn: Connection, filial_id: int, train_end: date, exclude_inactive: bool = True
) -> FilialSeries | None:
    """exclude_inactive: real forecasts skip products deactivated today. A backtest passes
    False — the product's CURRENT status is information from after train_end, so using it
    would leak the future into a forecast made "as of" a past date; there, only the data up
    to train_end (a sale in the last 8 weeks) decides."""
    train_start = conn.execute(
        text("SELECT min(snapshot_date) FROM analytics.daily_stock_snapshot WHERE filial_id = :f"),
        {"f": filial_id},
    ).scalar()
    if train_start is None or train_start > train_end:
        return None
    dates = [train_start + timedelta(days=i) for i in range((train_end - train_start).days + 1)]
    position = {d: i for i, d in enumerate(dates)}

    # Products with at least one sale in the last 8 weeks of the window (and active today,
    # unless this is a backtest — see the docstring).
    recent_start = train_end - timedelta(days=RECENT_SALES_DAYS - 1)
    product_rows = conn.execute(
        text("""
            SELECT p.id, p.category_id, c.department_id
            FROM public.products p
            JOIN public.categories c ON c.id = p.category_id
            WHERE p.filial_id = :f AND (:all_statuses OR p.status = 'active')
              AND EXISTS (SELECT 1 FROM analytics.daily_sales s
                          WHERE s.product_id = p.id AND s.sale_date BETWEEN :recent AND :end AND s.quantity > 0)
            ORDER BY p.id
        """),
        {"f": filial_id, "recent": recent_start, "end": train_end, "all_statuses": not exclude_inactive},
    ).all()
    products = {
        pid: ProductSeries(pid, cat, dep, [None] * len(dates), [None] * len(dates)) for pid, cat, dep in product_rows
    }
    if not products:
        return FilialSeries(filial_id, dates, {})

    ids = list(products)
    for pid, day, out_of_stock in conn.execute(
        text("""
            SELECT product_id, snapshot_date, was_out_of_stock FROM analytics.daily_stock_snapshot
            WHERE filial_id = :f AND product_id = ANY(:ids) AND snapshot_date BETWEEN :start AND :end
        """),
        {"f": filial_id, "ids": ids, "start": train_start, "end": train_end},
    ):
        if not out_of_stock:  # uncensored day: starts at 0 sold, filled below if it sold
            products[pid].quantity[position[day]] = 0.0
            products[pid].revenue[position[day]] = 0.0

    for pid, day, quantity, revenue in conn.execute(
        text("""
            SELECT product_id, sale_date, quantity, revenue FROM analytics.daily_sales
            WHERE filial_id = :f AND product_id = ANY(:ids) AND sale_date BETWEEN :start AND :end
        """),
        {"f": filial_id, "ids": ids, "start": train_start, "end": train_end},
    ):
        i = position[day]
        if products[pid].quantity[i] is not None:  # censored days stay None
            products[pid].quantity[i] = float(quantity)
            products[pid].revenue[i] = float(revenue)

    return FilialSeries(filial_id, dates, products)
