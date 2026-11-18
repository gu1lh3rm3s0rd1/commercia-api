"""What the AI consultant may DO, after the user answers yes (Fase 12, Etapa C).

Each action: typed params, a permission, `prepare` (validates against the store's data at
proposal time and writes the exact summary the user reads — by code, not by the model) and
`execute` (runs through the same services as the panel, as the user who said yes).

Deliberately NOT here (decided 2026-11-18): users, profiles, passwords, the store's plan,
registering sales, and creating/deleting records — the consultant acts on strategy: offers,
prices, costs, a draft purchase for restocking, and its own alerts.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import formatting as fmt
from app.core.deps import has_permission
from app.core.people import SUPPLIER
from app.core.timezone import local_today
from app.models.agent import AgentAlert
from app.models.catalog import Price, Product
from app.models.enums import RecordStatus
from app.models.sales import Person
from app.models.user import User
from app.modules.catalog import service as catalog
from app.modules.catalog.schemas import Money
from app.modules.purchases import service as purchases
from app.modules.purchases.schemas import PurchaseCreate, PurchaseItemCreate

EXPIRES_AFTER = timedelta(minutes=15)


class ActionError(Exception):
    """The proposal can't be made (explained to the model, which tells the user)."""


@dataclass
class Prepared:
    filial_id: int
    summary: str
    params: dict[str, Any]  # normalised, JSON-ready — exactly what will run


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


def _product(db: Session, product_id: int, stores: set[int]) -> Product:
    product = db.get(Product, product_id)
    if product is None or product.filial_id not in stores:
        raise ActionError(f"produto {product_id} não encontrado nas lojas do usuário")
    if product.status != RecordStatus.ACTIVE:
        raise ActionError(f"o produto {product.name} está inativo")
    return product


def _current_price(db: Session, product: Product) -> Price:
    price = catalog.get_current_price(db, product.id)
    if price is None:
        raise ActionError(f"o produto {product.name} não tem preço vigente")
    return price


def _no_future_price(db: Session, product: Product) -> None:
    future = db.execute(select(Price.id).where(Price.product_id == product.id, Price.start_date > local_today())).first()
    if future is not None:
        raise ActionError(f"o produto {product.name} já tem um preço agendado para o futuro; ajuste pelo painel")


def _cost(db: Session, product: Product) -> Decimal:
    cost = catalog.get_product_cost(db, product.id)
    return cost.cost_value if cost is not None else Decimal("0")


def _margin(price: Decimal, cost: Decimal) -> str:
    if not cost or not price:
        return "custo não cadastrado"
    return f"margem de {fmt.share((price - cost) / price)}"


# ---------------------------------------------------------------------
# set_offer — promotional price until a date, then back to the normal price
# ---------------------------------------------------------------------


class SetOffer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int
    offer_price: Money = Field(description="preço promocional")
    until: date = Field(description="último dia da oferta (AAAA-MM-DD)")


def _prepare_offer(db: Session, stores: set[int], a: SetOffer) -> Prepared:
    product = _product(db, a.product_id, stores)
    price = _current_price(db, product)
    today = local_today()
    if a.until < today or a.until > today + timedelta(days=60):
        raise ActionError("a oferta deve terminar entre hoje e daqui a 60 dias")
    if not 0 < a.offer_price < price.price_default:
        raise ActionError(f"a oferta precisa ser maior que zero e menor que o preço atual ({fmt.money(price.price_default)})")
    _no_future_price(db, product)
    cost = _cost(db, product)
    off = (price.price_default - a.offer_price) / price.price_default
    warning = " ATENÇÃO: abaixo do custo." if cost and a.offer_price < cost else ""
    summary = (f"Criar oferta para {product.name}: de {fmt.money(price.price_default)} por {fmt.money(a.offer_price)} "
               f"({fmt.share(off)} de desconto), de hoje até {fmt.day(a.until)}; depois volta a "
               f"{fmt.money(price.price_default)}. Custo {fmt.money(cost)}, {_margin(a.offer_price, cost)} na oferta.{warning}")
    return Prepared(product.filial_id, summary, {"product_id": product.id, "offer_price": str(a.offer_price),
                                                 "until": a.until.isoformat(), "price_default": str(price.price_default)})


def _execute_offer(db: Session, user: User, p: dict) -> dict:
    product = db.get(Product, p["product_id"])
    default, offer, until = Decimal(p["price_default"]), Decimal(p["offer_price"]), date.fromisoformat(p["until"])
    catalog.set_price(db, product, default, offer, local_today(), None)
    catalog.set_price(db, product, default, Decimal("0"), until + timedelta(days=1), None)  # back to normal after
    return {"product": product.name, "offer_price": str(offer), "until": p["until"]}


# ---------------------------------------------------------------------
# end_offer / set_price / set_cost
# ---------------------------------------------------------------------


class EndOffer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int


def _prepare_end_offer(db: Session, stores: set[int], a: EndOffer) -> Prepared:
    product = _product(db, a.product_id, stores)
    price = _current_price(db, product)
    if price.price_offer <= 0:
        raise ActionError(f"o produto {product.name} não está em oferta")
    _no_future_price(db, product)
    return Prepared(product.filial_id,
                    f"Encerrar a oferta de {product.name} hoje: o preço volta de {fmt.money(price.price_offer)} para "
                    f"{fmt.money(price.price_default)}.",
                    {"product_id": product.id, "price_default": str(price.price_default)})


def _execute_end_offer(db: Session, user: User, p: dict) -> dict:
    product = db.get(Product, p["product_id"])
    catalog.set_price(db, product, Decimal(p["price_default"]), Decimal("0"), local_today(), None)
    return {"product": product.name, "price": p["price_default"]}


class SetPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int
    new_price: Money = Field(description="novo preço normal, a partir de hoje")


def _prepare_price(db: Session, stores: set[int], a: SetPrice) -> Prepared:
    product = _product(db, a.product_id, stores)
    price = _current_price(db, product)
    if a.new_price <= 0:
        raise ActionError("o preço precisa ser maior que zero")
    if a.new_price == price.price_default:
        raise ActionError("o preço proposto é igual ao atual")
    _no_future_price(db, product)
    cost = _cost(db, product)
    change = (a.new_price - price.price_default) / price.price_default
    offer_note = f" A oferta atual de {fmt.money(price.price_offer)} é encerrada." if price.price_offer > 0 else ""
    warning = " ATENÇÃO: abaixo do custo." if cost and a.new_price < cost else ""
    return Prepared(product.filial_id,
                    f"Mudar o preço de {product.name} de {fmt.money(price.price_default)} para {fmt.money(a.new_price)} "
                    f"({fmt.pct(change)}) a partir de hoje. Custo {fmt.money(cost)}, {_margin(a.new_price, cost)}."
                    f"{offer_note}{warning}",
                    {"product_id": product.id, "new_price": str(a.new_price)})


def _execute_price(db: Session, user: User, p: dict) -> dict:
    product = db.get(Product, p["product_id"])
    catalog.set_price(db, product, Decimal(p["new_price"]), Decimal("0"), local_today(), None)
    return {"product": product.name, "price": p["new_price"]}


class SetCost(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int
    cost_value: Money


def _prepare_cost(db: Session, stores: set[int], a: SetCost) -> Prepared:
    product = _product(db, a.product_id, stores)
    current = _cost(db, product)
    if a.cost_value == current:
        raise ActionError("o custo proposto é igual ao atual")
    return Prepared(product.filial_id,
                    f"Mudar o custo de {product.name} de {fmt.money(current)} para {fmt.money(a.cost_value)}.",
                    {"product_id": product.id, "cost_value": str(a.cost_value)})


def _execute_cost(db: Session, user: User, p: dict) -> dict:
    product = db.get(Product, p["product_id"])
    catalog.set_product_cost(db, product.id, Decimal(p["cost_value"]))
    return {"product": product.name, "cost": p["cost_value"]}


# ---------------------------------------------------------------------
# draft_purchase — a PENDING purchase for restocking (someone receives it later)
# ---------------------------------------------------------------------


class DraftItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int
    quantity: int = Field(gt=0, le=10_000)
    unit_cost: Money | None = Field(None, description="omita para usar o custo atual do produto")


class DraftPurchase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_id: int
    items: list[DraftItem] = Field(min_length=1, max_length=20)


def _prepare_purchase(db: Session, stores: set[int], a: DraftPurchase) -> Prepared:
    supplier = db.get(Person, a.supplier_id)
    if supplier is None or supplier.filial_id not in stores or SUPPLIER not in supplier.roles \
            or supplier.status != RecordStatus.ACTIVE:
        raise ActionError(f"fornecedor {a.supplier_id} não encontrado ou inativo nas lojas do usuário")
    lines, items, total = [], [], Decimal("0")
    for it in a.items:
        product = _product(db, it.product_id, {supplier.filial_id})
        unit = it.unit_cost if it.unit_cost is not None else _cost(db, product)
        lines.append(f"{it.quantity} un. de {product.name} a {fmt.money(unit)}")
        items.append({"product_id": product.id, "quantity": it.quantity, "unit_cost": str(unit)})
        total += unit * it.quantity
    return Prepared(supplier.filial_id,
                    f"Criar compra PENDENTE de {supplier.display_name}: " + "; ".join(lines) +
                    f". Total {fmt.money(total)}. O estoque só entra quando alguém receber a compra no painel.",
                    {"supplier_id": supplier.id, "items": items})


def _execute_purchase(db: Session, user: User, p: dict) -> dict:
    data = PurchaseCreate(supplier_id=p["supplier_id"], items=[PurchaseItemCreate(**i) for i in p["items"]],
                          notes="Criada pelo agente de IA, confirmada no chat.", receive=False)
    supplier = db.get(Person, p["supplier_id"])
    purchase, _ = purchases.create_purchase(db, supplier.filial_id, user.id, data)
    return {"purchase_id": purchase.id, "total": str(purchase.total), "status": purchase.status.value}


# ---------------------------------------------------------------------
# update_alerts — the agent's own alerts
# ---------------------------------------------------------------------


class UpdateAlerts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alert_ids: list[int] = Field(min_length=1, max_length=20)
    status: Literal["seen", "dismissed"]


def _prepare_alerts(db: Session, stores: set[int], a: UpdateAlerts) -> Prepared:
    alerts = db.execute(select(AgentAlert).where(AgentAlert.id.in_(a.alert_ids))).scalars().all()
    if len(alerts) != len(set(a.alert_ids)) or any(x.filial_id not in stores for x in alerts):
        raise ActionError("algum alerta não foi encontrado nas lojas do usuário")
    if len({x.filial_id for x in alerts}) > 1:
        raise ActionError("uma ação por loja: trate os alertas de cada loja separadamente")
    word = "vistos" if a.status == "seen" else "dispensados"
    return Prepared(alerts[0].filial_id,
                    f"Marcar {len(alerts)} alerta(s) como {word}: " + "; ".join(x.title for x in alerts) + ".",
                    {"alert_ids": sorted(set(a.alert_ids)), "status": a.status})


def _execute_alerts(db: Session, user: User, p: dict) -> dict:
    now = datetime.now(timezone.utc)
    for alert in db.execute(select(AgentAlert).where(AgentAlert.id.in_(p["alert_ids"]))).scalars():
        alert.status, alert.handled_by_id, alert.handled_at = p["status"], user.id, now
    db.commit()
    return {"alerts": len(p["alert_ids"]), "status": p["status"]}


# ---------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------


@dataclass
class Action:
    name: str
    label: str
    permission: str
    params: type[BaseModel]
    prepare: Callable[[Session, set[int], Any], Prepared]
    execute: Callable[[Session, User, dict], dict]


ACTIONS: dict[str, Action] = {a.name: a for a in [
    Action("set_offer", "criar oferta (preço promocional até uma data)", "products.edit", SetOffer, _prepare_offer, _execute_offer),
    Action("end_offer", "encerrar oferta", "products.edit", EndOffer, _prepare_end_offer, _execute_end_offer),
    Action("set_price", "mudar o preço normal", "products.edit", SetPrice, _prepare_price, _execute_price),
    Action("set_cost", "mudar o custo", "products.edit", SetCost, _prepare_cost, _execute_cost),
    Action("draft_purchase", "criar compra pendente de reposição", "purchases.manage", DraftPurchase,
           _prepare_purchase, _execute_purchase),
    Action("update_alerts", "marcar alertas como vistos ou dispensados", "agent.view", UpdateAlerts,
           _prepare_alerts, _execute_alerts),
]}


def prepare(db: Session, user: User, stores: set[int], action: str, raw: dict) -> Prepared:
    spec = ACTIONS.get(action)
    if spec is None:
        raise ActionError(f"ação desconhecida: {action}. Ações possíveis: {', '.join(ACTIONS)}")
    if not has_permission(db, user, spec.permission):
        raise ActionError(f"o usuário não tem permissão para {spec.label}; apenas sugira, sem propor a ação")
    try:
        params = spec.params.model_validate(raw or {})
    except ValidationError as exc:
        raise ActionError("parâmetros inválidos — " + "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())) from exc
    return spec.prepare(db, stores, params)


def execute(db: Session, user: User, action: str, params: dict) -> dict:
    """Runs a confirmed action. Permission is checked again (it may have changed in 15 min)."""
    spec = ACTIONS[action]
    if not has_permission(db, user, spec.permission):
        raise HTTPException(status_code=403, detail=f"sem permissão para {spec.label}")
    return spec.execute(db, user, params)
