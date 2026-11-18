"""The assistant's tools (Fase 11): typed, read-only wrappers around the queries the panel
already uses — so the chat and the panel always show the same figure. The model picks a
tool and fills its arguments; it never computes a number or writes SQL.

Security: every tool runs with the asking user's access. `filial_id` is an argument the
model may choose, but only among the user's own stores (validated here, server-side);
with one store it's implied. Results carry no personal data (no CPF/e-mail/hashes) and
come pre-formatted in pt-BR (app/ai/formatting.py).

Freshness: analytics/forecasting hold COMPLETE days only (up to yesterday); "today" comes
from the live dashboard queries (get_today, get_sales_summary, compare_stores, ...).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.ai import formatting as fmt
from app.ai.periods import CompareWith, PeriodError, PeriodName, comparison, resolve
from app.ai.providers.base import ToolCall, ToolSpec
from app.analytics import queries as aq
from app.core.timezone import local_day_bounds, local_today
from app.forecasting import evaluation
from app.forecasting import queries as fq
from app.models.catalog import Category, Department, Product
from app.models.user import User
from app.modules.dashboard import service as dashboard

log = logging.getLogger(__name__)


class ToolError(Exception):
    """A problem the model should read and react to (ask the user, fix an argument)."""


@dataclass
class ToolContext:
    db: Session
    user: User
    stores: dict[int, str]  # the user's stores the assistant may see (Hulk plan): id -> name
    outside_plan: dict[int, str] | None = None  # the user's other stores (plan without the Assistente IA)
    proposal: tuple | None = None  # (action name, Prepared) — set by a propose_* tool, one per answer

    def store(self, filial_id: int | None) -> int:
        """One store for a per-store tool."""
        if filial_id is not None:
            if self.outside_plan and filial_id in self.outside_plan:
                raise ToolError(f"A loja {self.outside_plan[filial_id]} está no plano Pro: o Assistente IA "
                                "só acessa lojas do plano Hulk.")
            if filial_id not in self.stores:
                raise ToolError(f"A loja {filial_id} não está entre as lojas deste usuário: {self._list()}.")
            return filial_id
        if len(self.stores) == 1:
            return next(iter(self.stores))
        raise ToolError(f"O usuário tem {len(self.stores)} lojas ({self._list()}); informe filial_id "
                        "(ou pergunte ao usuário qual loja).")

    def stores_or_all(self, filial_id: int | None) -> set[int]:
        """For live tools that can sum stores: one store, or all of the user's."""
        return {self.store(filial_id)} if filial_id is not None else set(self.stores)

    def scope_label(self, filial_ids: set[int]) -> str:
        if len(filial_ids) == 1:
            return self.stores[next(iter(filial_ids))]
        return "todas as lojas (" + ", ".join(self.stores[f] for f in sorted(filial_ids)) + ")"

    def _list(self) -> str:
        return ", ".join(f"{fid} = {name}" for fid, name in sorted(self.stores.items()))


# ---------------------------------------------------------------------
# shared argument pieces
# ---------------------------------------------------------------------

Level = Literal["product", "category", "department", "filial"]
StoreArg = Field(None, description="Id da loja. Omita se o usuário tiver uma loja só.")
PeriodArg = Field("last_28_days", description="Período por nome; 'custom' exige start_date e end_date. "
                                              "last_N_days = os N dias completos até ontem; semanas de segunda a domingo.")
DateArg = Field(None, description="AAAA-MM-DD, só com period='custom'")


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _PeriodArgs(_Args):
    period: PeriodName = PeriodArg
    start_date: date | None = DateArg
    end_date: date | None = DateArg

    def dates(self) -> tuple[date, date]:
        try:
            return resolve(self.period, self.start_date, self.end_date)
        except PeriodError as exc:
            raise ToolError(str(exc)) from exc


def _complete_days(start: date, end: date) -> tuple[date, date, str | None]:
    """Analytics only has complete days (up to yesterday): clip, and say so."""
    yesterday = local_today() - timedelta(days=1)
    if start > yesterday:
        raise ToolError("Esta ferramenta só tem dias completos (até ontem). Para hoje, use get_today ou get_sales_summary.")
    if end > yesterday:
        return start, yesterday, "Inclui só dias completos: hoje não entra (para hoje use get_today)."
    return start, end, None


def _entity(ctx: ToolContext, filial_id: int, level: str, entity_id: int | None) -> aq.Entity:
    try:
        return aq.resolve_entity(ctx.db, filial_id, level, entity_id)
    except aq.EntityNotFound as exc:
        raise ToolError(f"{exc}. Use find_entities para achar o id pelo nome.") from exc


def _totals_view(t: dict) -> dict:
    return {
        "periodo": fmt.period(t["start_date"], t["end_date"]),
        "quantidade_vendida": fmt.qty(t["quantity"]),
        "faturamento": fmt.money(t["revenue"]),
        "custo": fmt.money(t["cost"]),
        "margem": fmt.money(t["margin"]),
        "margem_pct": fmt.share(t["margin_pct"]),
    }


# ---------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------


class FindEntitiesArgs(_Args):
    query: str = Field(min_length=2, max_length=80, description="Parte do nome, ex.: 'cerveja', 'coca'")
    level: Literal["product", "category", "department"] = "product"
    filial_id: int | None = StoreArg


def find_entities(ctx: ToolContext, a: FindEntitiesArgs) -> dict:
    pattern = f"%{a.query.strip()}%"
    if a.level == "department":
        rows = ctx.db.execute(select(Department.id, Department.name).where(Department.name.ilike(pattern))
                              .order_by(Department.name).limit(10)).all()
        return {"resultados": [{"id": r.id, "nome": r.name, "nivel": "department"} for r in rows]}
    filiais = ctx.stores_or_all(a.filial_id)
    model, store_col = (Product, Product.filial_id) if a.level == "product" else (Category, Category.company_id)
    rows = ctx.db.execute(
        select(model.id, model.name, model.status, store_col.label("filial_id"))
        .where(store_col.in_(filiais), model.name.ilike(pattern))
        .order_by(model.status, model.name, model.id).limit(15)
    ).all()
    return {"resultados": [
        {"id": r.id, "nome": r.name, "nivel": a.level, "loja": ctx.stores[r.filial_id], "filial_id": r.filial_id,
         "ativo": r.status.value == "active"}
        for r in rows
    ], "obs": None if rows else "Nada encontrado com esse nome."}


PAYMENT_NAMES = {"cash": "Dinheiro", "debit_card": "Cartão de débito", "credit_card": "Cartão de crédito",
                 "pix": "Pix", "other": "Outro"}


class SalesSummaryArgs(_PeriodArgs):
    filial_id: int | None = Field(None, description="Id da loja; omita para somar todas as lojas do usuário.")


def get_sales_summary(ctx: ToolContext, a: SalesSummaryArgs) -> dict:
    start, end = a.dates()
    filiais = ctx.stores_or_all(a.filial_id)
    r = dashboard.get_sales_summary(ctx.db, filiais, start, end)
    return {
        "lojas": ctx.scope_label(filiais),
        "periodo": fmt.period(start, end),
        "pedidos": r.total_orders,
        "faturamento_liquido": fmt.money(r.total_revenue),
        "descontos_concedidos": fmt.money(r.total_discount),
        "ticket_medio": fmt.money(r.average_ticket),
        "pedidos_cancelados": r.cancelled_orders,
        "taxa_cancelamento": fmt.share(r.cancellation_rate / 100),  # the dashboard gives it in percent points
        "por_forma_pagamento": [
            {"forma": PAYMENT_NAMES.get(p.payment_method.value, p.payment_method.value), "pedidos": p.count,
             "total": fmt.money(p.total)} for p in r.by_payment_method
        ],
    }


class TodayArgs(_Args):
    filial_id: int | None = Field(None, description="Id da loja; omita para somar todas as lojas do usuário.")


def get_today(ctx: ToolContext, a: TodayArgs) -> dict:
    filiais = ctx.stores_or_all(a.filial_id)
    r = dashboard.get_indicators(ctx.db, filiais)
    return {
        "lojas": ctx.scope_label(filiais),
        "hoje": fmt.day(local_today()),
        "vendas_hoje": r.sales_today.count,
        "faturamento_hoje": fmt.money(r.sales_today.total),
        "clientes_novos_hoje": r.new_clients_today,
        "produtos_ativos": r.active_products,
        "valor_em_estoque_a_custo": fmt.money(r.stock_value),
        "ultimos_7_dias": [{"dia": fmt.day(p.date), "vendas": p.count, "total": fmt.money(p.total)} for p in r.last_7_days],
    }


class ProductSalesArgs(_PeriodArgs):
    level: Level = Field(description="product, category, department ou filial (a loja inteira)")
    entity_id: int | None = Field(None, description="Id vindo de find_entities; omita para level='filial'")
    filial_id: int | None = StoreArg


def get_product_sales(ctx: ToolContext, a: ProductSalesArgs) -> dict:
    filial = ctx.store(a.filial_id)
    entity = _entity(ctx, filial, a.level, a.entity_id)
    start, end, note = _complete_days(*a.dates())
    return {"loja": ctx.stores[filial], "item": entity.name, "nivel": entity.level,
            **_totals_view(aq.sales_totals(ctx.db, filial, entity, start, end)), "obs": note}


class CompareArgs(ProductSalesArgs):
    compare_with: CompareWith = Field("previous_period", description="previous_period = período anterior de mesmo "
                                      "tamanho; same_period_last_year = mesmas datas no ano anterior")


def compare_sales_periods(ctx: ToolContext, a: CompareArgs) -> dict:
    filial = ctx.store(a.filial_id)
    entity = _entity(ctx, filial, a.level, a.entity_id)
    start, end, note = _complete_days(*a.dates())
    c_start, c_end = comparison(start, end, a.compare_with)
    r = aq.sales_compare(ctx.db, filial, entity, start, end, c_start, c_end)
    return {
        "loja": ctx.stores[filial], "item": entity.name, "nivel": entity.level,
        "atual": _totals_view(r["current"]),
        "comparacao": _totals_view(r["previous"]),
        "variacao": {k: fmt.pct(v) for k, v in r["change_pct"].items()},
        "obs": note,
    }


class RankingArgs(_PeriodArgs):
    level: Literal["product", "category", "department"] = "product"
    metric: Literal["revenue", "quantity", "margin"] = "revenue"
    limit: int = Field(10, ge=1, le=15)
    filial_id: int | None = StoreArg


def get_sales_ranking(ctx: ToolContext, a: RankingArgs) -> dict:
    filial = ctx.store(a.filial_id)
    start, end, note = _complete_days(*a.dates())
    rows = aq.sales_ranking(ctx.db, filial, a.level, start, end, a.metric, a.limit)
    return {
        "loja": ctx.stores[filial], "periodo": fmt.period(start, end), "ordenado_por": a.metric,
        "ranking": [
            {"posicao": i, "id": r["id"], "nome": r["name"], "quantidade": fmt.qty(r["quantity"]),
             "faturamento": fmt.money(r["revenue"]), "margem": fmt.money(r["margin"]),
             "participacao_no_total": fmt.share(r["share_pct"])}
            for i, r in enumerate(rows, start=1)
        ],
        "obs": note,
    }


class TrendArgs(ProductSalesArgs):
    pass


def get_sales_trend(ctx: ToolContext, a: TrendArgs) -> dict:
    filial = ctx.store(a.filial_id)
    entity = _entity(ctx, filial, a.level, a.entity_id)
    start, end, note = _complete_days(*a.dates())
    try:
        start, end = aq.resolve_period(ctx.db, filial, start, end)
    except aq.InvalidPeriod as exc:
        raise ToolError(str(exc)) from exc
    days = aq.sales_daily(ctx.db, filial, entity, start, end)
    if len(days) <= 35:
        points = [{"dia": fmt.day(d["date"]), "quantidade": fmt.qty(d["quantity"]), "faturamento": fmt.money(d["revenue"])}
                  for d in days]
        grain = "dia"
    else:  # long periods go weekly: fewer tokens, easier to read
        weeks: dict[date, dict] = {}
        for d in days:
            monday = d["date"] - timedelta(days=d["date"].weekday())
            w = weeks.setdefault(monday, {"quantity": 0, "revenue": 0})
            w["quantity"] += d["quantity"]
            w["revenue"] += d["revenue"]
        points = [{"semana_de": fmt.day(k), "quantidade": fmt.qty(v["quantity"]), "faturamento": fmt.money(v["revenue"])}
                  for k, v in sorted(weeks.items())]
        grain = "semana (segunda a domingo)"
    return {"loja": ctx.stores[filial], "item": entity.name, "periodo": fmt.period(start, end), "agrupado_por": grain,
            "pontos": points, "obs": note}


class LowStockArgs(_Args):
    threshold: int = Field(5, ge=0, le=1000, description="Mostra produtos ativos com estoque <= este número")
    filial_id: int | None = Field(None, description="Id da loja; omita para todas as lojas do usuário.")


def get_low_stock(ctx: ToolContext, a: LowStockArgs) -> dict:
    filiais = ctx.stores_or_all(a.filial_id)
    items, total = dashboard.get_low_stock(ctx.db, filiais, a.threshold, 30, 0)
    return {"lojas": ctx.scope_label(filiais), "estoque_ate": a.threshold, "total_de_produtos": total,
            "produtos": [{"id": i.product_id, "nome": i.name, "estoque_atual": i.quantity} for i in items],
            "obs": "Lista limitada aos 30 primeiros." if total > 30 else None}


class StockoutsArgs(_PeriodArgs):
    filial_id: int | None = StoreArg


def get_stockouts(ctx: ToolContext, a: StockoutsArgs) -> dict:
    filial = ctx.store(a.filial_id)
    start, end, note = _complete_days(*a.dates())
    rows = aq.stockouts(ctx.db, filial, start, end, limit=20)
    return {"loja": ctx.stores[filial], "periodo": fmt.period(start, end), "produtos_que_faltaram": [
        {"id": r["product_id"], "nome": r["product_name"], "categoria": r["category_name"], "dias_sem_estoque": r["days_out"],
         "ultima_falta": fmt.day(r["last_out_date"]), "estoque_atual": r["current_qty"]} for r in rows
    ], "obs": note}


class CompareStoresArgs(_PeriodArgs):
    pass


def compare_stores(ctx: ToolContext, a: CompareStoresArgs) -> dict:
    start, end = a.dates()
    items, _ = dashboard.get_sales_by_filial(ctx.db, set(ctx.stores), start, end, 50, 0)
    return {"periodo": fmt.period(start, end), "lojas": [
        {"filial_id": i.filial_id, "loja": i.filial_name, "pedidos": i.total_orders, "faturamento": fmt.money(i.total_revenue)}
        for i in items
    ], "obs": None if len(ctx.stores) > 1 else "O usuário tem uma loja só."}


class SellersArgs(_PeriodArgs):
    filial_id: int | None = Field(None, description="Id da loja; omita para todas as lojas do usuário.")


def get_sales_by_seller(ctx: ToolContext, a: SellersArgs) -> dict:
    start, end = a.dates()
    filiais = ctx.stores_or_all(a.filial_id)
    items, _ = dashboard.get_sales_by_seller(ctx.db, filiais, start, end, 20, 0)
    names = dict(ctx.db.execute(select(User.id, User.full_name).where(User.id.in_([i.user_id for i in items]))).all())
    return {"lojas": ctx.scope_label(filiais), "periodo": fmt.period(start, end), "vendedores": [
        {"vendedor": names.get(i.user_id) or i.username, "pedidos": i.total_orders, "faturamento": fmt.money(i.total_revenue)}
        for i in items
    ]}


class ForecastArgs(_Args):
    level: Level = "filial"
    entity_id: int | None = Field(None, description="Id vindo de find_entities; omita para level='filial'")
    filial_id: int | None = StoreArg


def forecast_sales(ctx: ToolContext, a: ForecastArgs) -> dict:
    filial = ctx.store(a.filial_id)
    entity = _entity(ctx, filial, a.level, a.entity_id)
    try:
        r = fq.forecast_summary(ctx.db, filial, entity)
    except fq.NoForecast as exc:
        raise ToolError("Ainda não há previsão gerada para esta loja.") from exc
    out = {"loja": ctx.stores[filial], "item": entity.name, "nivel": entity.level,
           "baseada_em_dados_ate": fmt.day(r["data_until"])}
    for target, t in r["targets"].items():
        fmt_value = fmt.qty if target == "quantity" else fmt.money
        out["quantidade" if target == "quantity" else "faturamento"] = {
            "modelo": t["model"],
            "proximos_7_dias": fmt_value(t["next_7_days"]),
            "proximos_14_dias": fmt_value(t["next_14_days"]),
            "ultimos_7_dias_reais": fmt_value(t["last_7_days"]),
            "ultimos_14_dias_reais": fmt_value(t["last_14_days"]),
            "variacao_7_dias": fmt.pct(t["change_7_days_pct"]),
            "variacao_14_dias": fmt.pct(t["change_14_days_pct"]),
            "erro_medio_tipico_dias_1_a_7": fmt.share(t["expected_wape_days_1_7"]),
            "erro_medio_tipico_dias_8_a_14": fmt.share(t["expected_wape_days_8_14"]),
        }
    return out


class AccuracyArgs(_Args):
    target: Literal["quantity", "revenue"] = "quantity"
    level: Literal["product", "category", "department", "filial"] = "filial"
    filial_id: int | None = StoreArg


def get_forecast_accuracy(ctx: ToolContext, a: AccuracyArgs) -> dict:
    filial = ctx.store(a.filial_id)
    rows = [r for r in evaluation.accuracy_report(filial, a.target, a.level) if r["wape"] is not None]
    if not rows:
        raise ToolError("Ainda não há avaliação das previsões para esta loja.")
    best: dict[str, dict] = {}
    for r in rows:  # ordered by wape within each bucket
        best.setdefault(r["horizon_bucket"], r)
    return {"loja": ctx.stores[filial], "alvo": a.target, "nivel": a.level, "melhor_modelo_por_horizonte": [
        {"horizonte_dias": bucket, "modelo": r["model"], "erro_medio_wape": fmt.share(r["wape"]),
         "vies": fmt.pct(r["bias"]), "rodadas_avaliadas": r["n_runs"]}
        for bucket, r in sorted(best.items(), key=lambda kv: int(kv[0].split("-")[0].rstrip("+")))
    ], "como_ler": "WAPE = erro médio em relação ao total vendido; viés negativo = a previsão costuma ficar abaixo do real."}


class StatusArgs(_Args):
    filial_id: int | None = StoreArg


def get_data_status(ctx: ToolContext, a: StatusArgs) -> dict:
    filial = ctx.store(a.filial_id)
    s = aq.status(ctx.db, filial)
    return {"loja": ctx.stores[filial], "analises_com_dados_ate": fmt.day(s["last_day"]),
            "ultima_venda_registrada_nas_analises": fmt.day(s["last_sale_date"]),
            "previsao_baseada_em_dados_ate": fmt.day(s["forecast_data_until"]),
            "obs": "Vendas de hoje aparecem só nas ferramentas ao vivo (get_today, get_sales_summary)."}




# ---------------------------------------------------------------------
# consultant tools (Fase 12, Etapa C): more data for strategies, and proposing ONE action
# ---------------------------------------------------------------------


class AlertsArgs(_Args):
    status: Literal["new", "seen", "dismissed"] = "new"
    filial_id: int | None = Field(None, description="Id da loja; omita para todas as lojas do usuário.")


def get_alerts(ctx: ToolContext, a: AlertsArgs) -> dict:
    from app.models.agent import AgentAlert  # local: avoids a models import cycle at startup

    filiais = ctx.stores_or_all(a.filial_id)
    rows = ctx.db.execute(select(AgentAlert).where(AgentAlert.filial_id.in_(filiais), AgentAlert.status == a.status)
                          .order_by(AgentAlert.reference_date.desc(), AgentAlert.id.desc()).limit(15)).scalars().all()
    return {"lojas": ctx.scope_label(filiais), "alertas": [
        {"id": r.id, "dia": fmt.day(r.reference_date), "gravidade": r.severity, "titulo": r.title, "detalhe": r.message,
         "produto_id": r.entity_id if r.entity_type == "product" else None} for r in rows
    ], "obs": None if rows else "Nenhum alerta com esse status."}


class RestockArgs(_Args):
    days: int = Field(7, ge=1, le=30, description="Lista produtos cujo estoque dura menos que isto")
    filial_id: int | None = StoreArg


def get_restock_suggestions(ctx: ToolContext, a: RestockArgs) -> dict:
    import math

    filial = ctx.store(a.filial_id)
    run = fq.pick_run(ctx.db, filial, "quantity", None)
    if run is None:
        raise ToolError("Ainda não há previsão gerada para esta loja.")
    start = local_today()
    rows = ctx.db.execute(text("""
        SELECT p.id, p.name, COALESCE(s.quantity, 0) AS stock, avg(fc.predicted) AS daily, COALESCE(c.cost_value, 0) AS cost
        FROM products p LEFT JOIN stocks s ON s.product_id = p.id LEFT JOIN product_costs c ON c.product_id = p.id
        JOIN forecasting.forecasts fc ON fc.run_id = :r AND fc.level = 'product' AND fc.entity_id = p.id
             AND fc.target_date BETWEEN :a AND :b
        WHERE p.filial_id = :f AND p.status = 'active'
        GROUP BY p.id, p.name, s.quantity, c.cost_value HAVING avg(fc.predicted) >= 0.5
    """), {"r": run["id"], "a": start, "b": start + timedelta(days=6), "f": filial}).mappings().all()
    short = sorted(((Decimal(r["stock"]) / Decimal(r["daily"]), r) for r in rows), key=lambda x: x[0])
    items = []
    for cover, r in short:
        if cover >= a.days:
            break
        buy = max(0, math.ceil(Decimal(r["daily"]) * 14 - r["stock"]))
        items.append({"produto_id": r["id"], "produto": r["name"], "estoque": r["stock"],
                      "demanda_prevista_dia": fmt.qty(round(Decimal(r["daily"]), 1)),
                      "dura_dias": fmt.qty(round(cover, 1)), "comprar_para_14_dias": buy,
                      "custo_estimado": fmt.money(Decimal(r["cost"]) * buy)})
    return {"loja": ctx.stores[filial], "estoque_que_dura_menos_de_dias": a.days, "produtos": items[:20],
            "obs": "Demanda pela previsão dos próximos 7 dias." if items else "Nenhum produto com estoque curto."}


class MarginArgs(_PeriodArgs):
    filial_id: int | None = Field(None, description="Id da loja; omita para todas as lojas do usuário.")


def get_margin(ctx: ToolContext, a: MarginArgs) -> dict:
    start, end = a.dates()
    filiais = ctx.stores_or_all(a.filial_id)
    summary = dashboard.get_margin_summary(ctx.db, filiais, start, end)
    items, _ = dashboard.get_margin_by_product(ctx.db, filiais, start, end, 200, 0)
    worst = sorted((i for i in items if i.revenue > 0), key=lambda i: i.margin_pct)[:8]
    return {"lojas": ctx.scope_label(filiais), "periodo": fmt.period(start, end),
            "faturamento": fmt.money(summary.total_revenue), "custo": fmt.money(summary.total_cost),
            "margem": fmt.money(summary.gross_margin),
            "margem_pct": fmt.share(summary.margin_pct / 100),  # the dashboard gives margins in percent points
            "produtos_com_menor_margem": [
                {"produto_id": i.product_id, "produto": i.name, "vendidos": i.quantity_sold, "faturamento": fmt.money(i.revenue),
                 "margem": fmt.money(i.margin), "margem_pct": fmt.share(i.margin_pct / 100)} for i in worst]}


class TopClientsArgs(_PeriodArgs):
    limit: int = Field(10, ge=1, le=20)
    filial_id: int | None = Field(None, description="Id da loja; omita para todas as lojas do usuário.")


def get_top_clients(ctx: ToolContext, a: TopClientsArgs) -> dict:
    start, end = a.dates()
    filiais = ctx.stores_or_all(a.filial_id)
    items, total = dashboard.get_sales_by_client(ctx.db, filiais, start, end, a.limit, 0)
    return {"lojas": ctx.scope_label(filiais), "periodo": fmt.period(start, end), "clientes_que_compraram": total,
            "melhores_clientes": [{"cliente": i.client_name, "pedidos": i.total_orders, "faturamento": fmt.money(i.total_revenue),
                                   "ticket_medio": fmt.money(i.average_ticket)} for i in items]}


class InactiveClientsArgs(_Args):
    days: int = Field(60, ge=7, le=365, description="Sem comprar há pelo menos tantos dias")
    filial_id: int | None = Field(None, description="Id da loja; omita para todas as lojas do usuário.")


def get_inactive_clients(ctx: ToolContext, a: InactiveClientsArgs) -> dict:
    filiais = ctx.stores_or_all(a.filial_id)
    items, total = dashboard.get_inactive_clients(ctx.db, filiais, a.days, 15, 0)
    return {"lojas": ctx.scope_label(filiais), "sem_comprar_ha_dias": a.days, "total_de_clientes": total,
            "clientes": [{"cliente": i.client_name, "ultima_compra": fmt.day(i.last_order_at),
                          "dias_sem_comprar": i.days_since_last_order} for i in items],
            "obs": "Só os 15 primeiros; nunca compraram aparecem primeiro." if total > 15 else None}


class PurchasesArgs(_PeriodArgs):
    filial_id: int | None = Field(None, description="Id da loja; omita para todas as lojas do usuário.")


def get_purchases_summary(ctx: ToolContext, a: PurchasesArgs) -> dict:
    start, end = a.dates()
    filiais = ctx.stores_or_all(a.filial_id)
    s, e = local_day_bounds(start)[0], local_day_bounds(end)[1]
    rows = ctx.db.execute(text("""
        SELECT COALESCE(x.trade_name, x.corporate_name, x.first_name || ' ' || x.last_name) AS supplier,
               count(*) AS purchases, sum(p.total) AS total, count(*) FILTER (WHERE p.status = 'pending') AS pending
        FROM purchases p JOIN people x ON x.id = p.supplier_id
        WHERE p.filial_id = ANY(:f) AND p.status <> 'cancelled'
          AND COALESCE(p.occurred_at, p.created_at) >= :s AND COALESCE(p.occurred_at, p.created_at) < :e
        GROUP BY 1 ORDER BY sum(p.total) DESC LIMIT 15
    """), {"f": list(filiais), "s": s, "e": e}).mappings().all()
    return {"lojas": ctx.scope_label(filiais), "periodo": fmt.period(start, end), "por_fornecedor": [
        {"fornecedor": r["supplier"], "compras": r["purchases"], "pendentes": r["pending"], "total": fmt.money(r["total"])}
        for r in rows], "obs": None if rows else "Nenhuma compra no período."}


class PatternsArgs(_PeriodArgs):
    filial_id: int | None = Field(None, description="Id da loja; omita para todas as lojas do usuário.")


def get_sales_patterns(ctx: ToolContext, a: PatternsArgs) -> dict:
    start, end = a.dates()
    filiais = ctx.stores_or_all(a.filial_id)
    weekdays = dashboard.get_sales_by_weekday(ctx.db, filiais, start, end)
    hours = [h for h in dashboard.get_sales_by_hour(ctx.db, filiais, start, end) if h.count]
    total = sum((w.total for w in weekdays), Decimal("0"))
    return {"lojas": ctx.scope_label(filiais), "periodo": fmt.period(start, end),
            "por_dia_da_semana": [{"dia": w.weekday_name, "pedidos": w.count, "faturamento": fmt.money(w.total),
                                   "participacao": fmt.share(w.total / total) if total else None,
                                   "ticket_medio": fmt.money(w.total / w.count) if w.count else None} for w in weekdays],
            "horas_de_maior_movimento": [{"hora": f"{h.hour:02d}h", "pedidos": h.count, "faturamento": fmt.money(h.total)}
                                         for h in sorted(hours, key=lambda h: h.total, reverse=True)[:5]]}


class ProductInfoArgs(_Args):
    product_id: int = Field(description="Id vindo de find_entities")


def get_product_info(ctx: ToolContext, a: ProductInfoArgs) -> dict:
    product = ctx.db.get(Product, a.product_id)
    if product is None or product.filial_id not in ctx.stores:
        raise ToolError("Produto não encontrado nas lojas do usuário.")
    from app.modules.catalog import service as catalog  # local: catalog imports nothing from here

    price = catalog.get_current_price(ctx.db, product.id)
    cost = catalog.get_product_cost(ctx.db, product.id)
    stock = catalog.get_stock(ctx.db, product)
    sold = ctx.db.execute(text("""SELECT COALESCE(sum(quantity), 0), COALESCE(sum(revenue), 0) FROM analytics.daily_sales
                                  WHERE product_id = :p AND sale_date >= :d"""),
                          {"p": product.id, "d": local_today() - timedelta(days=30)}).one()
    selling = None
    if price is not None:
        selling = price.price_offer if price.price_offer > 0 else price.price_default
    cost_value = cost.cost_value if cost else Decimal("0")
    return {"produto_id": product.id, "produto": product.name, "loja": ctx.stores[product.filial_id],
            "ativo": product.status.value == "active",
            "preco_normal": fmt.money(price.price_default) if price else None,
            "oferta": fmt.money(price.price_offer) if price and price.price_offer > 0 else None,
            "preco_vigente_ate": fmt.day(price.end_date) if price else None,
            "custo": fmt.money(cost_value),
            "margem_no_preco_praticado": fmt.share((selling - cost_value) / selling) if selling and cost_value else None,
            "estoque": stock.quantity if stock else 0,
            "vendidos_ultimos_30_dias": fmt.qty(sold[0]), "faturamento_ultimos_30_dias": fmt.money(sold[1])}


def _proposer(action_name: str):
    def propose(ctx: ToolContext, a) -> dict:
        from app.agent import actions  # local: the actions module imports this one's services

        if ctx.proposal is not None:
            raise ToolError("Já existe uma proposta nesta resposta: uma ação por vez.")
        try:
            prepared = actions.prepare(ctx.db, ctx.user, set(ctx.stores), action_name, a.model_dump(mode="json"))
        except actions.ActionError as exc:
            raise ToolError(str(exc)) from exc
        ctx.proposal = (action_name, prepared)
        return {"proposta": prepared.summary,
                "instrucao": "Apresente a proposta ao usuário com esse texto e termine perguntando: "
                             "'Posso fazer isso? Responda sim ou não.' Nada foi feito ainda."}
    return propose


# ---------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------


@dataclass
class Tool:
    name: str
    description: str
    args: type[_Args]
    fn: Callable[[ToolContext, Any], dict]

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, _clean_schema(self.args.model_json_schema()))


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("find_entities", "Acha o id de produtos, categorias ou departamentos pelo nome. Use antes das ferramentas "
         "que pedem entity_id.", FindEntitiesArgs, find_entities),
    Tool("get_today", "Vendas de HOJE (ao vivo), clientes novos hoje, produtos ativos, valor do estoque e os últimos 7 dias.",
         TodayArgs, get_today),
    Tool("get_sales_summary", "Resumo de vendas de um período (ao vivo, inclui hoje): pedidos, faturamento líquido, "
         "ticket médio, descontos, cancelamentos e formas de pagamento. Pode somar todas as lojas.",
         SalesSummaryArgs, get_sales_summary),
    Tool("get_product_sales", "Quantidade, faturamento, custo e margem de um produto, categoria, departamento ou da "
         "loja inteira num período (dias completos).", ProductSalesArgs, get_product_sales),
    Tool("compare_sales_periods", "Compara um período com o anterior (ou com o mesmo período do ano passado) para um "
         "produto, categoria, departamento ou a loja inteira.", CompareArgs, compare_sales_periods),
    Tool("get_sales_ranking", "Ranking dos produtos, categorias ou departamentos que mais venderam (por faturamento, "
         "quantidade ou margem) num período.", RankingArgs, get_sales_ranking),
    Tool("get_sales_trend", "Evolução das vendas dia a dia (ou por semana em períodos longos).", TrendArgs, get_sales_trend),
    Tool("get_low_stock", "Produtos ativos com estoque baixo AGORA.", LowStockArgs, get_low_stock),
    Tool("get_stockouts", "Produtos que ficaram sem estoque em algum dia do período.", StockoutsArgs, get_stockouts),
    Tool("compare_stores", "Compara as lojas do usuário lado a lado (pedidos e faturamento) num período.",
         CompareStoresArgs, compare_stores),
    Tool("get_sales_by_seller", "Vendas por vendedor num período.", SellersArgs, get_sales_by_seller),
    Tool("forecast_sales", "Previsão de vendas (quantidade e faturamento) para os próximos 7 e 14 dias, com o erro "
         "típico do modelo. Para a loja, uma categoria, departamento ou produto.", ForecastArgs, forecast_sales),
    Tool("get_forecast_accuracy", "O quanto as previsões costumam errar (avaliação retroativa).", AccuracyArgs,
         get_forecast_accuracy),
    Tool("get_data_status", "Até que dia vão os dados das análises e das previsões.", StatusArgs, get_data_status),
    # consultant (Fase 12)
    Tool("get_alerts", "Alertas do agente (vendas fora do esperado, ruptura, produto parado, venda abaixo do custo, "
         "cancelamentos). Use para 'o que preciso saber hoje?'.", AlertsArgs, get_alerts),
    Tool("get_restock_suggestions", "Produtos cujo estoque dura poucos dias pela previsão, com quanto comprar para 14 dias.",
         RestockArgs, get_restock_suggestions),
    Tool("get_margin", "Margem da loja no período e os produtos com menor margem.", MarginArgs, get_margin),
    Tool("get_top_clients", "Melhores clientes do período (pedidos, faturamento, ticket médio).", TopClientsArgs, get_top_clients),
    Tool("get_inactive_clients", "Clientes que não compram há N dias (para ações de reativação).", InactiveClientsArgs,
         get_inactive_clients),
    Tool("get_purchases_summary", "Compras por fornecedor no período.", PurchasesArgs, get_purchases_summary),
    Tool("get_sales_patterns", "Vendas por dia da semana e horários de maior movimento, com ticket médio.", PatternsArgs,
         get_sales_patterns),
    Tool("get_product_info", "Preço, oferta, custo, margem, estoque e vendas dos últimos 30 dias de um produto. Use antes de "
         "propor oferta ou preço.", ProductInfoArgs, get_product_info),
]}


def _register_proposals() -> None:
    """One propose_<action> tool per executable action (app/agent/actions.py): typed params,
    so the model fills them like any other tool. Proposing runs nothing."""
    from app.agent.actions import ACTIONS

    for name, action in ACTIONS.items():
        TOOLS[f"propose_{name}"] = Tool(
            f"propose_{name}",
            f"PROPÕE (não executa) {action.label}. O sistema mostra o resumo exato e só executa se o usuário responder "
            "'sim'. Uma proposta por resposta.",
            action.params, _proposer(name))


_register_proposals()


def _clean_schema(schema: dict) -> dict:
    """Pydantic JSON schema → the plain subset function-calling APIs handle well: no titles,
    no $ref/$defs (inlined), `X | None` collapsed to X (optional = not in `required`), money
    (Decimal: number-or-string) as a number, dates as strings."""
    defs = schema.get("$defs", {})

    def clean(node):
        if isinstance(node, list):
            return [clean(x) for x in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            node = {**defs[node["$ref"].split("/")[-1]], **{k: v for k, v in node.items() if k != "$ref"}}
        node = {k: clean(v) for k, v in node.items() if k not in ("title", "$defs", "additionalProperties")}
        any_of = node.get("anyOf")
        if any_of:
            options = [x for x in any_of if x != {"type": "null"}]
            numeric = [x for x in options if x.get("type") == "number"]
            if len(options) == 1 or (numeric and all(x.get("type") in ("number", "string") for x in options)):
                chosen = numeric[0] if numeric else options[0]  # Decimal → number (the string form is only for parsing)
                node = {**{k: v for k, v in node.items() if k != "anyOf"}, **chosen}
                if node.get("default") is None:
                    node.pop("default", None)
        if node.get("format") == "date" and "AAAA-MM-DD" not in node.get("description", ""):
            node["description"] = (node.get("description", "") + " (AAAA-MM-DD)").strip()
        return node

    return clean(schema)


def specs() -> list[ToolSpec]:
    return [t.spec() for t in TOOLS.values()]


def run(ctx: ToolContext, call: ToolCall) -> dict:
    """Never raises: errors go back to the model as {"erro": ...} so it can fix the call or
    explain to the user. The transaction is read-only, but an SQL error still needs a rollback."""
    tool = TOOLS.get(call.name)
    if tool is None:
        return {"erro": f"ferramenta desconhecida: {call.name}"}
    try:
        args = tool.args.model_validate(call.args or {})
        return tool.fn(ctx, args)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return {"erro": f"argumentos inválidos — {problems}"}
    except ToolError as exc:
        return {"erro": str(exc)}
    except (aq.EntityNotFound, aq.InvalidPeriod) as exc:
        return {"erro": str(exc)}
    except Exception:
        log.exception("tool %s failed", call.name)
        ctx.db.rollback()
        return {"erro": "falha interna ao consultar os dados"}


def store_names(db: Session, filial_ids: set[int]) -> dict[int, str]:
    from app.models.company import Company

    rows = db.execute(
        select(Company.id, func.coalesce(Company.fantasy_name, Company.corporate_name)).where(Company.id.in_(filial_ids))
    ).all()
    return {fid: name for fid, name in rows}
