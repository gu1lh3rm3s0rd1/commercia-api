"""Read queries over the `analytics` schema — the single implementation behind the panel's
/api/v1/analytics routes AND, later, the AI agent's tools (Fase 11): the agent never
computes numbers or writes SQL itself, it calls these, so the panel and the agent always
show the same figure.

Conventions shared by every function:
- scoped to one filial; `level` is product | category | department | filial, and the
  entity must belong to that filial (EntityNotFound otherwise);
- periods default to the last 28 days ending on the filial's last day with a sale (days
  after it are "no data", not zero sales) and can't exceed 366 days (InvalidPeriod);
- names come with ids (charts need labels, the agent needs to say "Cervejas");
- percentages are fractions (0.15 = 15%); formatting is the caller's job.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.timezone import local_today

LEVELS = ("product", "category", "department", "filial")
DEFAULT_PERIOD_DAYS = 28
MAX_PERIOD_DAYS = 366


class EntityNotFound(LookupError):
    pass


class InvalidPeriod(ValueError):
    pass


@dataclass
class Entity:
    level: str
    id: int
    name: str


# daily_sales s joined with p (products) and c (categories): how each level filters it
_LEVEL_FILTER = {
    "product": "s.product_id = :e",
    "category": "p.category_id = :e",
    "department": "c.department_id = :e",
    "filial": "TRUE",
}
_SALES_FROM = """
    FROM analytics.daily_sales s
    JOIN public.products p ON p.id = s.product_id
    JOIN public.categories c ON c.id = p.category_id
"""


def ratio(numerator, denominator) -> Decimal | None:
    if not denominator:
        return None
    return (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.0001"))


def resolve_entity(db: Session, filial_id: int, level: str, entity_id: int | None) -> Entity:
    """Validates that the entity exists in this filial and returns it with its name.
    For level 'filial' the entity is the filial itself (entity_id optional)."""
    if level not in LEVELS:
        raise EntityNotFound(f"unknown level '{level}' — options: {', '.join(LEVELS)}")
    if level == "filial":
        if entity_id not in (None, filial_id):
            raise EntityNotFound(f"filial {entity_id} is not filial {filial_id}")
        name = db.execute(
            text("SELECT coalesce(fantasy_name, corporate_name) FROM public.companies WHERE id = :f"), {"f": filial_id}
        ).scalar()
        return Entity("filial", filial_id, name or f"Filial {filial_id}")
    if entity_id is None:
        raise EntityNotFound(f"entity_id is required for level '{level}'")
    sql = {
        "product": "SELECT name FROM public.products WHERE id = :e AND filial_id = :f",
        "category": "SELECT name FROM public.categories WHERE id = :e AND company_id = :f",
        "department": "SELECT name FROM public.departments WHERE id = :e",  # departments are global
    }[level]
    name = db.execute(text(sql), {"e": entity_id, "f": filial_id}).scalar()
    if name is None:
        raise EntityNotFound(f"{level} {entity_id} not found in filial {filial_id}")
    return Entity(level, entity_id, name)


def last_sale_date(db: Session, filial_id: int) -> date | None:
    return db.execute(
        text("SELECT max(sale_date) FROM analytics.daily_sales WHERE filial_id = :f AND sale_date < :today"),
        {"f": filial_id, "today": local_today()},
    ).scalar()


def resolve_period(
    db: Session, filial_id: int, start_date: date | None, end_date: date | None, default_days: int = DEFAULT_PERIOD_DAYS
) -> tuple[date, date]:
    end = end_date or last_sale_date(db, filial_id) or (local_today() - timedelta(days=1))
    start = start_date or end - timedelta(days=default_days - 1)
    if start > end:
        raise InvalidPeriod("start_date must not be after end_date")
    if (end - start).days + 1 > MAX_PERIOD_DAYS:
        raise InvalidPeriod(f"period too long: at most {MAX_PERIOD_DAYS} days")
    return start, end


# ---------------------------------------------------------------------
# sales
# ---------------------------------------------------------------------


def sales_daily(db: Session, filial_id: int, entity: Entity, start: date, end: date) -> list[dict]:
    """Day-by-day quantity / revenue / cost / margin, dense (0 on days without a sale)."""
    rows = db.execute(
        text(f"""
            WITH sold AS (
                SELECT s.sale_date, sum(s.quantity) AS quantity, sum(s.revenue) AS revenue, sum(s.cost) AS cost
                {_SALES_FROM}
                WHERE s.filial_id = :f AND {_LEVEL_FILTER[entity.level]} AND s.sale_date BETWEEN :start AND :end
                GROUP BY s.sale_date
            )
            SELECT d::date AS date, coalesce(sold.quantity, 0) AS quantity,
                   coalesce(sold.revenue, 0) AS revenue, coalesce(sold.cost, 0) AS cost
            FROM generate_series(CAST(:start AS date), CAST(:end AS date), interval '1 day') AS d
            LEFT JOIN sold ON sold.sale_date = d::date
            ORDER BY d
        """),
        {"f": filial_id, "e": entity.id, "start": start, "end": end},
    ).mappings().all()
    return [{**r, "margin": r["revenue"] - r["cost"]} for r in rows]


def _totals(db: Session, filial_id: int, entity: Entity, start: date, end: date) -> dict:
    quantity, revenue, cost = db.execute(
        text(f"""
            SELECT coalesce(sum(s.quantity), 0), coalesce(sum(s.revenue), 0), coalesce(sum(s.cost), 0)
            {_SALES_FROM}
            WHERE s.filial_id = :f AND {_LEVEL_FILTER[entity.level]} AND s.sale_date BETWEEN :start AND :end
        """),
        {"f": filial_id, "e": entity.id, "start": start, "end": end},
    ).one()
    margin = revenue - cost
    return {
        "start_date": start, "end_date": end, "days": (end - start).days + 1,
        "quantity": quantity, "revenue": revenue, "cost": cost, "margin": margin,
        "margin_pct": ratio(margin, revenue),
    }


def sales_totals(db: Session, filial_id: int, entity: Entity, start: date, end: date) -> dict:
    """Quantity / revenue / cost / margin of one entity over one period (the chat's
    get_product_sales tool)."""
    return _totals(db, filial_id, entity, start, end)


def sales_compare(
    db: Session, filial_id: int, entity: Entity, start: date, end: date,
    compare_start: date | None = None, compare_end: date | None = None,
) -> dict:
    """Two periods side by side. Without an explicit comparison period, uses the one right
    before, of the same length."""
    if compare_start is None or compare_end is None:
        length = (end - start).days + 1
        compare_end = start - timedelta(days=1)
        compare_start = compare_end - timedelta(days=length - 1)
    current = _totals(db, filial_id, entity, start, end)
    previous = _totals(db, filial_id, entity, compare_start, compare_end)
    change = {
        key: ratio(current[key] - previous[key], previous[key])
        for key in ("quantity", "revenue", "cost", "margin")
    }
    return {"current": current, "previous": previous, "change_pct": change}


_RANK_KEYS = {
    "product": ("p.id", "p.name"),
    "category": ("c.id", "c.name"),
    "department": ("dep.id", "dep.name"),
}
RANK_METRICS = ("revenue", "quantity", "margin")


def sales_ranking(
    db: Session, filial_id: int, level: str, start: date, end: date, metric: str = "revenue", limit: int = 10
) -> list[dict]:
    """Entities of one level ordered by revenue, quantity or margin over a period, each
    with its share of the filial total for that metric."""
    if level not in _RANK_KEYS:
        raise EntityNotFound("ranking level must be product, category or department")
    if metric not in RANK_METRICS:
        raise InvalidPeriod(f"metric must be one of: {', '.join(RANK_METRICS)}")
    key, name = _RANK_KEYS[level]
    rows = db.execute(
        text(f"""
            WITH ranked AS (
                SELECT {key} AS id, {name} AS name, sum(s.quantity) AS quantity, sum(s.revenue) AS revenue,
                       sum(s.cost) AS cost, sum(s.revenue) - sum(s.cost) AS margin
                {_SALES_FROM}
                JOIN public.departments dep ON dep.id = c.department_id
                WHERE s.filial_id = :f AND s.sale_date BETWEEN :start AND :end
                GROUP BY {key}, {name}
            )
            SELECT *, sum({metric}) OVER () AS metric_total FROM ranked
            ORDER BY {metric} DESC, id LIMIT :limit
        """),
        {"f": filial_id, "start": start, "end": end, "limit": limit},
    ).mappings().all()
    return [
        {
            "level": level, "id": r["id"], "name": r["name"], "quantity": r["quantity"], "revenue": r["revenue"],
            "cost": r["cost"], "margin": r["margin"], "margin_pct": ratio(r["margin"], r["revenue"]),
            "share_pct": ratio(r[metric], r["metric_total"]),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------
# stock
# ---------------------------------------------------------------------


def stock_daily(db: Session, filial_id: int, product: Entity, start: date, end: date) -> list[dict]:
    rows = db.execute(
        text("""
            SELECT snapshot_date AS date, end_of_day_qty, min_qty, was_out_of_stock
            FROM analytics.daily_stock_snapshot
            WHERE filial_id = :f AND product_id = :p AND snapshot_date BETWEEN :start AND :end
            ORDER BY snapshot_date
        """),
        {"f": filial_id, "p": product.id, "start": start, "end": end},
    ).mappings().all()
    return [dict(r) for r in rows]


def stockouts(db: Session, filial_id: int, start: date, end: date, limit: int = 20) -> list[dict]:
    """Products that ran out of stock in the period, most days out first, with their
    current (latest snapshot) quantity."""
    rows = db.execute(
        text("""
            WITH outs AS (
                SELECT product_id, count(*) AS days_out, max(snapshot_date) AS last_out_date
                FROM analytics.daily_stock_snapshot
                WHERE filial_id = :f AND was_out_of_stock AND snapshot_date BETWEEN :start AND :end
                GROUP BY product_id
            ),
            latest AS (
                SELECT DISTINCT ON (product_id) product_id, end_of_day_qty
                FROM analytics.daily_stock_snapshot
                WHERE filial_id = :f AND product_id IN (SELECT product_id FROM outs)
                ORDER BY product_id, snapshot_date DESC
            )
            SELECT o.product_id, p.name AS product_name, c.id AS category_id, c.name AS category_name,
                   o.days_out, o.last_out_date, l.end_of_day_qty AS current_qty
            FROM outs o
            JOIN public.products p ON p.id = o.product_id
            JOIN public.categories c ON c.id = p.category_id
            LEFT JOIN latest l ON l.product_id = o.product_id
            ORDER BY o.days_out DESC, o.last_out_date DESC, o.product_id
            LIMIT :limit
        """),
        {"f": filial_id, "start": start, "end": end, "limit": limit},
    ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------
# freshness
# ---------------------------------------------------------------------


def status(db: Session, filial_id: int) -> dict:
    """How fresh the analytics and forecasts are — the panel's "dados até" footer, and what
    lets the agent warn that it's answering from stale data."""
    first_day, last_snapshot = db.execute(
        text("SELECT min(snapshot_date), max(snapshot_date) FROM analytics.daily_stock_snapshot WHERE filial_id = :f"),
        {"f": filial_id},
    ).one()
    last_job = db.execute(
        text("""SELECT job, status, started_at, finished_at, from_date, to_date FROM analytics.job_runs
                ORDER BY id DESC LIMIT 1""")
    ).mappings().one_or_none()
    last_success = db.execute(
        text("SELECT max(finished_at) FROM analytics.job_runs WHERE status = 'success'")
    ).scalar()
    forecast = db.execute(
        text("""SELECT max(created_at) AS last_forecast_at, max(train_end) AS forecast_data_until
                FROM forecasting.forecast_runs WHERE filial_id = :f AND status = 'success' AND kind = 'forecast'"""),
        {"f": filial_id},
    ).mappings().one()
    last_evaluated = db.execute(
        text("""SELECT max(e.evaluated_at) FROM forecasting.forecast_evaluations e
                JOIN forecasting.forecast_runs r ON r.id = e.run_id WHERE r.filial_id = :f"""),
        {"f": filial_id},
    ).scalar()
    return {
        "first_day": first_day,
        "last_day": last_snapshot,
        "last_sale_date": last_sale_date(db, filial_id),
        "last_job": dict(last_job) if last_job else None,
        "last_successful_job_at": last_success,
        "last_forecast_at": forecast["last_forecast_at"],
        "forecast_data_until": forecast["forecast_data_until"],
        "last_evaluated_at": last_evaluated,
    }
