import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

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


# --- products ---


class ProductCreate(BaseModel):
    sku: str
    name: str
    description: str | None = None
    brand: str | None = None
    barcode: str | None = None
    category_id: int
    weight: Decimal | None = None
    height: Decimal | None = None
    width: Decimal | None = None
    depth: Decimal | None = None


class ProductUpdate(BaseModel):
    """Partial update. Fields left as None are not changed — there is currently no way
    to explicitly clear an optional field (e.g. barcode) back to null via this endpoint;
    acceptable simplification for now, revisit if that becomes a real need."""

    name: str | None = None
    description: str | None = None
    brand: str | None = None
    barcode: str | None = None
    category_id: int | None = None
    weight: Decimal | None = None
    height: Decimal | None = None
    width: Decimal | None = None
    depth: Decimal | None = None


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


# --- product cost ---


class ProductCostSet(BaseModel):
    cost_value: Decimal


class ProductCostRead(ORMModel):
    product_id: int
    cost_value: Decimal
    updated_at: datetime


# --- prices ---


class PriceSet(BaseModel):
    price_default: Decimal
    price_offer: Decimal | None = None
    start_date: date | None = None
    end_date: date | None = None


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
