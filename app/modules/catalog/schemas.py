import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import RecordStatus

T = TypeVar("T")

# Team convention: price/cost values are never null — 0 instead, both in requests that
# omit them and in every response.
ZERO = Decimal("0.00")


def _null_as_zero(v):
    return ZERO if v is None else v


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


# --- product cost ---


class ProductCostSet(BaseModel):
    cost_value: Decimal = Field(ge=0)


class ProductCostRead(ORMModel):
    product_id: int
    cost_value: Decimal
    updated_at: datetime | None = None  # None only on the synthetic zero-cost row


# --- prices ---


class PriceSet(BaseModel):
    price_default: Decimal = Field(ge=0)
    price_offer: Decimal = Field(default=ZERO, ge=0)  # 0 = no offer
    start_date: date | None = None
    end_date: date | None = None

    _offer_null_as_zero = field_validator("price_offer", mode="before")(_null_as_zero)

    @model_validator(mode="after")
    def _check_business_rules(self) -> "PriceSet":
        # Mirrors the DB's own ck_prices_offer_lower / ck_prices_date_range checks —
        # catching it here gives a clean 422 instead of an unhandled IntegrityError (500).
        if self.price_offer > self.price_default:
            raise ValueError("price_offer must not exceed price_default")
        if self.start_date is not None and self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


class PriceRead(ORMModel):
    id: int | None = None  # None only on the synthetic zero-price row (product with no price yet)
    product_id: int
    filial_id: int
    price_default: Decimal
    price_offer: Decimal  # 0 = no offer
    start_date: date | None
    end_date: date | None
    created_at: datetime | None = None

    # Rows written before migration 0004 may still hold NULL — read them as 0.
    _offer_null_as_zero = field_validator("price_offer", mode="before")(_null_as_zero)


class ProductPriceRead(BaseModel):
    """One row per product with its price valid today (GET /catalog/prices). Price fields
    are 0 when the product has no current price — the app must treat price_default == 0
    as 'no price, can't be sold'."""

    product_id: int
    filial_id: int
    price_default: Decimal
    price_offer: Decimal
    start_date: date | None
    end_date: date | None


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
    # Convenience copies of data already exposed by their own dedicated endpoints
    # (.../prices/current, .../cost, .../stock) — those keep working unchanged, this just
    # saves callers (mobile sync included) from 3 extra round-trips per product.
    # Never null: a zero-valued row when nothing has been set (same idea as stock).
    current_price: PriceRead
    cost: ProductCostRead
    stock: StockRead
