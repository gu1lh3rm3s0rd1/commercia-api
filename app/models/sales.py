from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Computed, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.models.enums import OrderStatus, PaymentMethod, RecordStatus, StockMovementReason, pg_enum


class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (CheckConstraint("cpf ~ '^[0-9]{11}$'", name="ck_clients_cpf_format"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(120))
    last_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(160), unique=True)
    cpf: Mapped[str] = mapped_column(String(11), unique=True)
    birth_date: Mapped[date | None] = mapped_column(default=None)
    phone: Mapped[str | None] = mapped_column(String(20), default=None)
    status: Mapped[RecordStatus] = mapped_column(
        pg_enum(RecordStatus, "record_status"), default=RecordStatus.ACTIVE
    )
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    filial_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ClientAddress(Base):
    __tablename__ = "client_addresses"
    __table_args__ = (
        CheckConstraint("zip_code ~ '^[0-9]{8}$'", name="ck_client_addresses_zip_format"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"))
    street: Mapped[str] = mapped_column(String(200))
    number: Mapped[str | None] = mapped_column(String(20), default=None)
    complement: Mapped[str | None] = mapped_column(String(120), default=None)
    neighborhood: Mapped[str | None] = mapped_column(String(120), default=None)
    city: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(2))
    zip_code: Mapped[str] = mapped_column(String(8))


class Order(Base):
    """total is a snapshot already net of discount (SUM(order_items.total) - discount).
    The application must recalculate it whenever items change — never edited by hand."""

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("total >= 0", name="ck_orders_total"),
        CheckConstraint("discount >= 0", name="ck_orders_discount"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    discount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    payment_method: Mapped[PaymentMethod] = mapped_column(pg_enum(PaymentMethod, "payment_method"))
    status: Mapped[OrderStatus] = mapped_column(
        pg_enum(OrderStatus, "order_status"), default=OrderStatus.PENDING
    )
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    filial_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class OrderItem(Base):
    """quantity = actually fulfilled. original_qty is filled in only during mobile sync
    (Fase 5) when server-side stock is less than what was requested offline — it stays
    NULL in the normal case. cost_at_sale freezes product_costs.cost_value at sale time,
    the same way unit_price freezes the price — needed for correct margin analysis even
    if the product's cost changes later."""

    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity"),
        CheckConstraint(
            "original_qty IS NULL OR original_qty >= quantity", name="ck_order_items_original_qty"
        ),
        CheckConstraint("unit_price >= 0", name="ck_order_items_unit_price"),
        CheckConstraint("cost_at_sale >= 0", name="ck_order_items_cost_at_sale"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int]
    original_qty: Mapped[int | None] = mapped_column(default=None)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    cost_at_sale: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), Computed("quantity * unit_price", persisted=True)
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class StockMovement(Base):
    """Historical fact, not derived from anything else — what Analytics/Forecasting will
    use to tell a real demand drop apart from a stock-out. Never update stocks.quantity
    without writing the corresponding row here, in the same transaction."""

    __tablename__ = "stock_movements"
    __table_args__ = (
        CheckConstraint("change_qty <> 0", name="ck_stock_movements_change_qty"),
        CheckConstraint("resulting_qty >= 0", name="ck_stock_movements_resulting_qty"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    filial_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    change_qty: Mapped[int]
    reason: Mapped[StockMovementReason] = mapped_column(
        pg_enum(StockMovementReason, "stock_movement_reason")
    )
    resulting_qty: Mapped[int]
    order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_items.id", ondelete="SET NULL"), default=None
    )
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
