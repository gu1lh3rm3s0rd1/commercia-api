from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import PurchaseStatus
from app.modules.catalog.schemas import Money

INVOICE_KEY_PATTERN = r"^[0-9]{44}$"


class PurchaseItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int
    quantity: int = Field(gt=0)
    unit_cost: Money  # from the supplier's invoice — typed, unlike a sale's price


class PurchaseCreate(BaseModel):
    """`receive: true` creates it already received (stock in + cost now) — the usual case
    when the goods arrive together with the invoice. Otherwise it stays pending until
    POST /purchases/{id}/receive."""

    model_config = ConfigDict(extra="forbid")

    supplier_id: int
    items: list[PurchaseItemCreate] = Field(min_length=1)
    discount: Money = Decimal("0")
    freight: Money = Decimal("0")
    invoice_number: str | None = Field(None, min_length=1, max_length=20)
    invoice_key: str | None = Field(None, pattern=INVOICE_KEY_PATTERN, description="NF-e access key (44 digits)")
    issued_at: date | None = Field(None, description="invoice date")
    notes: str | None = Field(None, max_length=1000)
    receive: bool = False


class PurchaseItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    quantity: int
    unit_cost: Decimal
    total: Decimal  # quantity * unit_cost
    discount_allocated: Decimal
    freight_allocated: Decimal
    net_total: Decimal  # total - discount share + freight share
    landed_unit_cost: Decimal  # net_total / quantity — what becomes the product cost


class PurchaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    supplier_id: int
    filial_id: int
    status: PurchaseStatus
    total: Decimal  # items - discount + freight
    discount: Decimal
    freight: Decimal
    invoice_number: str | None
    invoice_key: str | None
    issued_at: date | None
    notes: str | None
    client_reference: UUID | None
    occurred_at: datetime | None
    received_at: datetime | None
    created_by_id: int | None
    received_by_id: int | None
    created_at: datetime
    updated_at: datetime
    items: list[PurchaseItemRead] = []
