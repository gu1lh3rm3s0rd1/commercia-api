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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import formatting as fmt
from app.ai.periods import CompareWith, PeriodError, PeriodName, comparison, resolve
from app.ai.providers.base import ToolCall, ToolSpec
from app.analytics import queries as aq
from app.core.timezone import local_today
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
    stores: dict[int, str]  # every store the user can access: id -> name

    def store(self, filial_id: int | None) -> int:
        """One store for a per-store tool."""
        if filial_id is not None:
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
        "taxa_cancelamento": fmt.share(r.cancellation_rate),
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
]}


def _clean_schema(schema: dict) -> dict:
    """Pydantic JSON schema → the plain subset function-calling APIs handle well: no
    titles, `X | None` collapsed to X (optional = not in `required`), dates as strings."""
    def clean(node):
        if isinstance(node, dict):
            node = {k: clean(v) for k, v in node.items() if k != "title"}
            any_of = node.get("anyOf")
            if any_of and len(any_of) == 2 and {"type": "null"} in any_of:
                rest = next(x for x in any_of if x != {"type": "null"})
                node = {**{k: v for k, v in node.items() if k != "anyOf"}, **rest}
                if node.get("default") is None:
                    node.pop("default", None)
            if node.get("format") == "date":
                node["description"] = (node.get("description", "") + " (AAAA-MM-DD)").strip()
            return node
        if isinstance(node, list):
            return [clean(x) for x in node]
        return node
    out = clean(schema)
    out.pop("additionalProperties", None)
    return out


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
