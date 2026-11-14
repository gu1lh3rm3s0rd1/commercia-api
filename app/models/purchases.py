"""Purchases from suppliers (Etapa 3, migration 0015) — the mirror of orders/order_items.

Differences from a sale: the counterpart is a person with the "supplier" role; the unit cost
is typed (from the supplier's invoice); stock only enters when the purchase is RECEIVED;
discount and freight are both split across the items. The landed unit cost of the latest
purchase (by when it happened) becomes the product's cost.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Computed, ForeignKey, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.models.enums import PurchaseStatus, pg_enum


class Purchase(Base):
    """total = SUM(item totals) - discount + freight = SUM(purchase_items.net_total).
    client_reference/occurred_at: only for purchases synced from the mobile app (same
    idempotency model as offline sales)."""

    __tablename__ = "purchases"
    __table_args__ = (
        CheckConstraint("total >= 0", name="ck_purchases_total"),
        CheckConstraint("discount >= 0", name="ck_purchases_discount"),
        CheckConstraint("freight >= 0", name="ck_purchases_freight"),
        CheckConstraint("invoice_key ~ '^[0-9]{44}$'", name="ck_purchases_invoice_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("people.id"))
    filial_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    status: Mapped[PurchaseStatus] = mapped_column(pg_enum(PurchaseStatus, "purchase_status"),
                                                   default=PurchaseStatus.PENDING)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    discount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    freight: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    invoice_number: Mapped[str | None] = mapped_column(String(20), default=None)
    invoice_key: Mapped[str | None] = mapped_column(String(44), default=None)  # NF-e access key
    issued_at: Mapped[date | None] = mapped_column(default=None)  # invoice date
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    client_reference: Mapped[uuid.UUID | None] = mapped_column(Uuid, unique=True, default=None)
    occurred_at: Mapped[datetime | None] = mapped_column(default=None)
    received_at: Mapped[datetime | None] = mapped_column(default=None)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    received_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class PurchaseItem(Base):
    __tablename__ = "purchase_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_purchase_items_quantity"),
        CheckConstraint("unit_cost >= 0", name="ck_purchase_items_unit_cost"),
        CheckConstraint("discount_allocated >= 0 AND discount_allocated <= quantity * unit_cost",
                        name="ck_purchase_items_discount"),
        CheckConstraint("freight_allocated >= 0", name="ck_purchase_items_freight"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_id: Mapped[int] = mapped_column(ForeignKey("purchases.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int]
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), Computed("quantity * unit_cost", persisted=True))
    discount_allocated: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    freight_allocated: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    net_total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), Computed("quantity * unit_cost - discount_allocated + freight_allocated", persisted=True)
    )
    landed_unit_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 4),
        Computed("(quantity * unit_cost - discount_allocated + freight_allocated) / quantity", persisted=True),
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
