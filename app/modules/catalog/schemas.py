import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import RecordStatus

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


# --- departments ---


class DepartmentCreate(BaseModel):
    name: str


class DepartmentRead(ORMModel):
    id: int
    name: str


# --- categories ---


class CategoryCreate(BaseModel):
    name: str
    department_id: int


class CategoryUpdate(BaseModel):
    name: str | None = None
    department_id: int | None = None


class CategoryRead(ORMModel):
    id: int
    name: str
    department_id: int
    company_id: int
    created_at: datetime
    updated_at: datetime


# --- products ---


class ProductCreate(BaseModel):
    sku: str
    name: str
    description: str | None = None
    brand: str | None = None
    barcode: str | None = None
    category_id: int
    weight: Decimal | None = Field(default=None, ge=0)
    height: Decimal | None = Field(default=None, ge=0)
    width: Decimal | None = Field(default=None, ge=0)
    depth: Decimal | None = Field(default=None, ge=0)


class ProductUpdate(BaseModel):
    """Partial update. Fields left as None are not changed — there is currently no way
    to explicitly clear an optional field (e.g. barcode) back to null via this endpoint;
    acceptable simplification for now, revisit if that becomes a real need."""

    name: str | None = None
    description: str | None = None
    brand: str | None = None
    barcode: str | None = None
    category_id: int | None = None
    weight: Decimal | None = Field(default=None, ge=0)
    height: Decimal | None = Field(default=None, ge=0)
    width: Decimal | None = Field(default=None, ge=0)
    depth: Decimal | None = Field(default=None, ge=0)


class ProductRead(ORMModel):
    id: int
    sku: str
    name: str
    description: str | None
    brand: str | None
    barcode: str | None
    category_id: int
    filial_id: int
    weight: Decimal | None
    height: Decimal | None
    width: Decimal | None
    depth: Decimal | None
    status: RecordStatus
    created_at: datetime
    updated_at: datetime


# --- product cost ---


class ProductCostSet(BaseModel):
    cost_value: Decimal = Field(ge=0)


class ProductCostRead(ORMModel):
    product_id: int
    cost_value: Decimal
    updated_at: datetime


# --- prices ---


class PriceSet(BaseModel):
    price_default: Decimal = Field(ge=0)
    price_offer: Decimal | None = Field(default=None, ge=0)
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def _check_business_rules(self) -> "PriceSet":
        # Mirrors the DB's own ck_prices_offer_lower / ck_prices_date_range checks —
        # catching it here gives a clean 422 instead of an unhandled IntegrityError (500).
        if self.price_offer is not None and self.price_offer > self.price_default:
            raise ValueError("price_offer must not exceed price_default")
        if self.start_date is not None and self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


class PriceRead(ORMModel):
    id: int
    product_id: int
    filial_id: int
    price_default: Decimal
    price_offer: Decimal | None
    start_date: date | None
    end_date: date | None
    created_at: datetime


# --- stock ---


class ManualStockReason(str, enum.Enum):
    """Deliberately excludes 'sale' — that reason is only ever written by the Vendas
    service (Fase 4), never through this manual-adjustment endpoint."""

    ADJUSTMENT = "adjustment"
    LOSS = "loss"
    RETURN = "return"
    RESTOCK = "restock"


class StockAdjust(BaseModel):
    quantity_delta: int = Field(..., description="Positive to add, negative to remove.")
    reason: ManualStockReason


class StockRead(ORMModel):
    product_id: int
    filial_id: int
    quantity: int
    updated_at: datetime | None = None
