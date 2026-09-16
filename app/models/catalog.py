from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.models.enums import RecordStatus, pg_enum


class Department(Base):
    """Shared across every store — confirmed not scoped per filial."""

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)


class Category(Base):
    """Scoped per store (company_id) — catalog is independent per filial, not shared."""

    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_categories_company_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Product(Base):
    """Catalog is per store. Invariant enforced by the service layer, not the DB:
    filial_id must equal categories.company_id of the referenced category_id."""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("weight IS NULL OR weight >= 0", name="ck_products_weight"),
        CheckConstraint("height IS NULL OR height >= 0", name="ck_products_height"),
        CheckConstraint("width IS NULL OR width >= 0", name="ck_products_width"),
        CheckConstraint("depth IS NULL OR depth >= 0", name="ck_products_depth"),
        UniqueConstraint("filial_id", "sku", name="uq_products_sku"),
        UniqueConstraint("filial_id", "barcode", name="uq_products_barcode"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(default=None)
    brand: Mapped[str | None] = mapped_column(String(120), default=None)
    barcode: Mapped[str | None] = mapped_column(String(30), default=None)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    filial_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    weight: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), default=None)
    height: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    width: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    depth: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    status: Mapped[RecordStatus] = mapped_column(
        pg_enum(RecordStatus, "record_status"), default=RecordStatus.ACTIVE
    )
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ProductCost(Base):
    """1:1 with products — current cost only. Historical cost-per-sale lives in
    order_items.cost_at_sale, frozen at sale time (see app/models/sales.py)."""

    __tablename__ = "product_costs"
    __table_args__ = (
        CheckConstraint("cost_value >= 0", name="ck_product_costs_value"),
        UniqueConstraint("product_id", name="uq_product_costs_product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    cost_value: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Price(Base):
    """Append-only log: a price change is an INSERT closing the previous row's end_date,
    never an UPDATE in place — enforced by the service layer, not the DB.
    filial_id must equal products.filial_id of product_id — also service-layer enforced
    (decision 2026-09-09: kept explicit here for query convenience despite redundancy)."""

    __tablename__ = "prices"
    __table_args__ = (
        CheckConstraint(
            "price_offer IS NULL OR price_offer <= price_default", name="ck_prices_offer_lower"
        ),
        CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR end_date >= start_date",
            name="ck_prices_date_range",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    filial_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    price_default: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    price_offer: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), default=None)
    start_date: Mapped[date | None] = mapped_column(default=None)
    end_date: Mapped[date | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Stock(Base):
    """Current-state snapshot only. Real history lives in stock_movements — never change
    quantity without writing the corresponding movement in the same transaction."""

    __tablename__ = "stocks"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_stocks_quantity"),
        UniqueConstraint("product_id", "filial_id", name="uq_stocks_product_filial"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    filial_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    quantity: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
