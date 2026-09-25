from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import OrderStatus, PaymentMethod


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class OrderItemCreate(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


class OrderCreate(BaseModel):
    client_id: int
    payment_method: PaymentMethod
    discount: Decimal = Field(default=Decimal("0"), ge=0)
    items: list[OrderItemCreate] = Field(min_length=1)
    # Only ever set by the Fase 5 mobile sync push — the online endpoint never sends these.
    client_reference: UUID | None = None
    occurred_at: datetime | None = None


class OrderItemRead(ORMModel):
    id: int
    product_id: int
    quantity: int
    original_qty: int | None
    unit_price: Decimal
    cost_at_sale: Decimal
    total: Decimal


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
