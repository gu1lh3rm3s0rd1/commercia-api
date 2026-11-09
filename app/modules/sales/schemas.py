from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import OrderStatus, PaymentMethod
from app.modules.catalog.schemas import Money


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class OrderItemCreate(BaseModel):
    """No price here on purpose: the unit price always comes from the product's price
    valid on the sale day (offer when > 0, otherwise the default price). Unknown fields are
    rejected (422) instead of silently dropped — a client sending e.g. unit_price must find
    out the API won't use it, rather than record a different value than it displayed."""

    model_config = ConfigDict(extra="forbid")

    product_id: int
    quantity: int = Field(gt=0)


class OrderCreate(BaseModel):
    """Online sale (POST /orders). client_reference/occurred_at are NOT accepted here —
    they belong to offline sales, which go through POST /sync/push."""

    model_config = ConfigDict(extra="forbid")

    client_id: int
    payment_method: PaymentMethod
    discount: Money = Decimal("0")
    items: list[OrderItemCreate] = Field(min_length=1)


class OrderItemRead(ORMModel):
    id: int
    product_id: int
    quantity: int
    original_qty: int | None
    unit_price: Decimal
    cost_at_sale: Decimal
    total: Decimal  # gross: quantity * unit_price
    discount_allocated: Decimal  # this line's share of the order discount
    net_total: Decimal  # total - discount_allocated; the lines' net_total add up to the order total


class OrderRead(ORMModel):
    id: int
    client_id: int
    filial_id: int
    total: Decimal
    discount: Decimal
    payment_method: PaymentMethod
    status: OrderStatus
    client_reference: UUID | None
    occurred_at: datetime | None
    created_at: datetime
    updated_at: datetime
    items: list[OrderItemRead] = []
