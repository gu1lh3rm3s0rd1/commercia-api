"""The agent's detectors (Fase 12, RF09): rules in code over analytics, forecasts and the
operational tables. Each returns AlertDrafts with pre-formatted pt-BR text and the numbers
behind them — the AI never computes or invents a figure here.

`day` is the business-timezone day being analysed (normally yesterday: analytics holds
complete days). Stock-based rules look at the stock as it is now.
"""

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ai import formatting as fmt
from app.analytics import queries as aq
from app.core.timezone import local_day_bounds
from app.forecasting import queries as fq

STOCKOUT_DAYS = 3  # stock that lasts less than this, by the forecast, is a risk
RESTOCK_DAYS = 14  # suggested purchase covers this many days of forecast demand
MIN_DAILY_DEMAND = Decimal("0.5")  # ignore products that barely sell (forecast noise)
STALE_QUIET_DAYS = 14  # a regular seller with no sale for this long...
STALE_HISTORY_DAYS = 60  # ...that sold on at least STALE_MIN_DAYS of the 60 days before
STALE_MIN_DAYS = 12
CANCEL_MIN = 3  # at least this many cancellations in the day...
CANCEL_FACTOR = 2  # ...and more than twice the daily average of the previous 28 days
FORECAST_MAX_AGE_DAYS = 8  # the forecast runs every Monday


@dataclass
class AlertDraft:
    kind: str
    severity: str  # info | warning | critical
    title: str
    message: str
    entity_type: str | None = None
    entity_id: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


KIND_LABELS = {
    "sales_outside_forecast": "Vendas fora do esperado",
    "stockout_risk": "Risco de faltar estoque",
    "stale_product": "Produto parado",
    "below_cost": "Venda abaixo do custo",
    "cancellation_spike": "Pico de cancelamentos",
    "stale_data": "Dados desatualizados",
}


def _rows(db: Session, sql: str, **params) -> list[dict]:
    return [dict(r) for r in db.execute(text(sql), params).mappings().all()]


# ---------------------------------------------------------------------
# 1. sales outside the forecast band
# ---------------------------------------------------------------------


def sales_outside_forecast(db: Session, filial_id: int, day: date) -> list[AlertDraft]:
    """Yesterday's revenue vs the 80% band of a forecast made BEFORE that day (the best
    model's latest forecast covering it; a backtest one when no real forecast covered it)."""
    best = fq.pick_run(db, filial_id, "revenue", None)
    if best is None:
        return []
    point = _rows(db, """
        SELECT f.predicted, f.lower, f.upper, r.kind FROM forecasting.forecasts f
        JOIN forecasting.forecast_runs r ON r.id = f.run_id
        WHERE r.filial_id = :f AND r.target = 'revenue' AND r.status = 'success' AND r.model = :m
          AND r.train_end < :d AND f.level = 'filial' AND f.entity_id = :f AND f.target_date = :d
        ORDER BY (r.kind = 'forecast') DESC, r.train_end DESC, r.id DESC LIMIT 1
    """, f=filial_id, m=best["model"], d=day)
    has_data = db.execute(text("SELECT max(sale_date) >= :d FROM analytics.daily_sales WHERE filial_id = :f"),
                          {"f": filial_id, "d": day}).scalar()
    if not point or not has_data:
        return []
    p = point[0]
    actual = db.execute(text("""SELECT COALESCE(sum(revenue), 0) FROM analytics.daily_sales
                                WHERE filial_id = :f AND sale_date = :d"""), {"f": filial_id, "d": day}).scalar()
    data = {"actual": str(actual), "predicted": str(p["predicted"]), "lower": str(p["lower"]),
            "upper": str(p["upper"]), "model": best["model"], "forecast_kind": p["kind"]}
    band = f"faixa prevista {fmt.money(p['lower'])} a {fmt.money(p['upper'])}, previsão {fmt.money(p['predicted'])}"
    if actual < p["lower"]:
        return [AlertDraft("sales_outside_forecast", "warning", f"Vendas de {fmt.day(day)} abaixo do esperado",
                           f"Faturamento de {fmt.money(actual)}, abaixo da {band}.", "filial", filial_id, data)]
    if actual > p["upper"]:
        return [AlertDraft("sales_outside_forecast", "info", f"Vendas de {fmt.day(day)} acima do esperado",
                           f"Faturamento de {fmt.money(actual)}, acima da {band}.", "filial", filial_id, data)]
    return []


# ---------------------------------------------------------------------
# 2. stock about to run out
# ---------------------------------------------------------------------


def stockout_risk(db: Session, filial_id: int, day: date) -> list[AlertDraft]:
    """Current stock ÷ the product's forecast daily demand for the next 7 days."""
    run = fq.pick_run(db, filial_id, "quantity", None)
    if run is None:
        return []
    start = day + timedelta(days=1)
    rows = _rows(db, """
        SELECT p.id, p.name, COALESCE(s.quantity, 0) AS stock, avg(fc.predicted) AS daily
        FROM products p
        LEFT JOIN stocks s ON s.product_id = p.id
        JOIN forecasting.forecasts fc ON fc.run_id = :r AND fc.level = 'product' AND fc.entity_id = p.id
             AND fc.target_date BETWEEN :a AND :b
        WHERE p.filial_id = :f AND p.status = 'active'
        GROUP BY p.id, p.name, s.quantity
        HAVING avg(fc.predicted) >= :min_daily
    """, r=run["id"], a=start, b=start + timedelta(days=6), f=filial_id, min_daily=MIN_DAILY_DEMAND)
    alerts = []
    for r in rows:
        daily, stock = Decimal(r["daily"]), int(r["stock"])
        cover = Decimal(stock) / daily
        if cover >= STOCKOUT_DAYS:
            continue
        suggest = max(0, math.ceil(daily * RESTOCK_DAYS - stock))
        data = {"stock": stock, "daily_demand": str(round(daily, 2)), "days_of_cover": str(round(cover, 1)),
                "suggested_purchase": suggest, "covers_days": RESTOCK_DAYS}
        if stock == 0:
            alerts.append(AlertDraft("stockout_risk", "critical", f"{r['name']} está sem estoque",
                                     f"Sem estoque, com demanda prevista de {fmt.qty(round(daily, 1))} un./dia. "
                                     f"Sugestão: comprar {suggest} un. para {RESTOCK_DAYS} dias.",
                                     "product", r["id"], data))
        else:
            alerts.append(AlertDraft("stockout_risk", "warning", f"{r['name']} pode faltar em breve",
                                     f"{stock} un. em estoque duram cerca de {fmt.qty(round(cover, 1))} dia(s) pela "
                                     f"previsão ({fmt.qty(round(daily, 1))} un./dia). Sugestão: comprar {suggest} un. "
                                     f"para {RESTOCK_DAYS} dias.", "product", r["id"], data))
    return alerts


# ---------------------------------------------------------------------
# 3. a regular seller that stopped selling
# ---------------------------------------------------------------------


def stale_product(db: Session, filial_id: int, day: date) -> list[AlertDraft]:
    quiet_from = day - timedelta(days=STALE_QUIET_DAYS - 1)
    rows = _rows(db, """
        WITH history AS (
            SELECT product_id, count(*) AS days, max(sale_date) AS last_sale FROM analytics.daily_sales
            WHERE filial_id = :f AND sale_date BETWEEN :h0 AND :h1 GROUP BY product_id
        ),
        recent AS (
            SELECT DISTINCT product_id FROM analytics.daily_sales
            WHERE filial_id = :f AND sale_date BETWEEN :q AND :d
        )
        SELECT p.id, p.name, s.quantity AS stock, h.days, h.last_sale
        FROM history h
        JOIN products p ON p.id = h.product_id AND p.status = 'active'
        JOIN stocks s ON s.product_id = p.id AND s.quantity > 0
        WHERE h.days >= :min_days AND h.product_id NOT IN (SELECT product_id FROM recent)
    """, f=filial_id, h0=quiet_from - timedelta(days=STALE_HISTORY_DAYS), h1=quiet_from - timedelta(days=1),
        q=quiet_from, d=day, min_days=STALE_MIN_DAYS)
    return [
        AlertDraft("stale_product", "info", f"{r['name']} parou de vender",
                   f"Vendeu em {r['days']} dos {STALE_HISTORY_DAYS} dias anteriores, mas não vende há "
                   f"{STALE_QUIET_DAYS} dias, com {r['stock']} un. em estoque. Vale conferir preço, exposição ou validade.",
                   "product", r["id"], {"sales_days": r["days"], "last_sale": fmt.day(r["last_sale"]), "stock": r["stock"]})
        for r in rows
    ]


# ---------------------------------------------------------------------
# 4. sold below cost
# ---------------------------------------------------------------------


def below_cost(db: Session, filial_id: int, day: date) -> list[AlertDraft]:
    start, end = local_day_bounds(day)
    rows = _rows(db, """
        SELECT i.product_id, p.name, sum(i.quantity) AS quantity, sum(i.net_total) AS revenue,
               sum(i.quantity * i.cost_at_sale) AS cost
        FROM order_items i
        JOIN orders o ON o.id = i.order_id
        JOIN products p ON p.id = i.product_id
        WHERE o.filial_id = :f AND o.status <> 'cancelled'
          AND COALESCE(o.occurred_at, o.created_at) >= :s AND COALESCE(o.occurred_at, o.created_at) < :e
        GROUP BY i.product_id, p.name
        HAVING sum(i.net_total) < sum(i.quantity * i.cost_at_sale)
    """, f=filial_id, s=start, e=end)
    return [
        AlertDraft("below_cost", "warning", f"{r['name']} vendido abaixo do custo",
                   f"Em {fmt.day(day)}: {r['quantity']} un. vendidas por {fmt.money(r['revenue'])}, com custo de "
                   f"{fmt.money(r['cost'])} (prejuízo de {fmt.money(r['cost'] - r['revenue'])}). Confira o preço, a "
                   f"oferta e os descontos.", "product", r["product_id"],
                   {"quantity": r["quantity"], "revenue": str(r["revenue"]), "cost": str(r["cost"])})
        for r in rows
    ]


# ---------------------------------------------------------------------
# 5. spike of cancellations
# ---------------------------------------------------------------------


def cancellation_spike(db: Session, filial_id: int, day: date) -> list[AlertDraft]:
    """Orders cancelled during the day (updated_at = when the cancellation was written) vs
    the daily average of the 28 days before."""
    start, end = local_day_bounds(day)
    before = local_day_bounds(day - timedelta(days=28))[0]
    count, average = db.execute(text("""
        SELECT count(*) FILTER (WHERE updated_at >= :s AND updated_at < :e),
               count(*) FILTER (WHERE updated_at >= :b AND updated_at < :s) / 28.0
        FROM orders WHERE filial_id = :f AND status = 'cancelled'
    """), {"f": filial_id, "s": start, "e": end, "b": before}).one()
    if count < CANCEL_MIN or count <= CANCEL_FACTOR * Decimal(average):
        return []
    return [AlertDraft("cancellation_spike", "warning", f"Muitos cancelamentos em {fmt.day(day)}",
                       f"{count} vendas canceladas no dia, contra uma média de {fmt.qty(round(average, 1))} por dia nas 4 "
                       f"semanas anteriores. Vale verificar o motivo.", "filial", filial_id,
                       {"cancelled": count, "daily_average": str(round(average, 2))})]


# ---------------------------------------------------------------------
# 6. stale data
# ---------------------------------------------------------------------


def stale_data(db: Session, filial_id: int, day: date) -> list[AlertDraft]:
    s = aq.status(db, filial_id)
    alerts = []
    if s["last_day"] is None or s["last_day"] < day:
        alerts.append(AlertDraft("stale_data", "warning", "Análises desatualizadas",
                                 f"As análises vão só até {fmt.day(s['last_day']) or 'nenhum dia'} (esperado: "
                                 f"{fmt.day(day)}). A rotina da madrugada pode não ter rodado.", None, None,
                                 {"analytics_until": fmt.day(s["last_day"])}))
    until = s["forecast_data_until"]
    if until is None or (day - until).days > FORECAST_MAX_AGE_DAYS:
        alerts.append(AlertDraft("stale_data", "warning", "Previsão desatualizada",
                                 f"A previsão usa dados até {fmt.day(until) or 'nenhum dia'}; ela é refeita toda "
                                 "segunda-feira.", None, -1, {"forecast_until": fmt.day(until)}))
    return alerts


DETECTORS = [sales_outside_forecast, stockout_risk, stale_product, below_cost, cancellation_spike, stale_data]
