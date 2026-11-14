import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Computed, ForeignKey, Numeric, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.models.enums import OrderStatus, PaymentMethod, PersonType, RecordStatus, StockMovementReason, pg_enum


class Person(Base):
    """Every registration of a store — clients, suppliers and whatever role comes next
    (migration 0013, was `clients`). `roles` says what the person is to the store, several
    at once allowed (a farmer who supplies eggs and also buys); the valid values live in
    app/core/people.py, so a new role needs no migration.

    individual (pessoa física) → cpf + first/last name; company (pessoa jurídica) → cnpj +
    corporate_name (trade_name optional) — enforced by ck_people_document. CPF/CNPJ are
    unique per store (UC004), not across stores: the same supplier serves many businesses."""

    __tablename__ = "people"
    __table_args__ = (
        CheckConstraint("cpf ~ '^[0-9]{11}$'", name="ck_people_cpf_format"),
        CheckConstraint("cnpj ~ '^[0-9]{14}$'", name="ck_people_cnpj_format"),
        CheckConstraint("cardinality(roles) >= 1", name="ck_people_roles_not_empty"),
        CheckConstraint(
            "(person_type = 'individual' AND cpf IS NOT NULL AND cnpj IS NULL"
            " AND first_name IS NOT NULL AND last_name IS NOT NULL)"
            " OR (person_type = 'company' AND cnpj IS NOT NULL AND cpf IS NULL AND corporate_name IS NOT NULL)",
            name="ck_people_document",
        ),
        UniqueConstraint("filial_id", "cpf", name="uq_people_filial_cpf"),
        UniqueConstraint("filial_id", "cnpj", name="uq_people_filial_cnpj"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    person_type: Mapped[PersonType] = mapped_column(pg_enum(PersonType, "person_type"), default=PersonType.INDIVIDUAL)
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text))
    first_name: Mapped[str | None] = mapped_column(String(120), default=None)
    last_name: Mapped[str | None] = mapped_column(String(120), default=None)
    corporate_name: Mapped[str | None] = mapped_column(String(160), default=None)
    trade_name: Mapped[str | None] = mapped_column(String(160), default=None)
    email: Mapped[str | None] = mapped_column(String(160), default=None)
    cpf: Mapped[str | None] = mapped_column(String(11), default=None)
    cnpj: Mapped[str | None] = mapped_column(String(14), default=None)
    birth_date: Mapped[date | None] = mapped_column(default=None)
    phone: Mapped[str | None] = mapped_column(String(20), default=None)
    status: Mapped[RecordStatus] = mapped_column(
        pg_enum(RecordStatus, "record_status"), default=RecordStatus.ACTIVE
    )
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    filial_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    @property
    def display_name(self) -> str:
        if self.person_type == PersonType.COMPANY:
            return self.trade_name or self.corporate_name or ""
        return f"{self.first_name or ''} {self.last_name or ''}".strip()


def person_display_name():
    """SQL version of Person.display_name, for reports that select names in bulk."""
    return func.coalesce(Person.trade_name, Person.corporate_name, Person.first_name + " " + Person.last_name)


class PersonAddress(Base):
    __tablename__ = "person_addresses"
    __table_args__ = (
        CheckConstraint("zip_code ~ '^[0-9]{8}$'", name="ck_person_addresses_zip_format"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"))
    street: Mapped[str] = mapped_column(String(200))
    number: Mapped[str | None] = mapped_column(String(20), default=None)
    complement: Mapped[str | None] = mapped_column(String(120), default=None)
    neighborhood: Mapped[str | None] = mapped_column(String(120), default=None)
    city: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(2))
    zip_code: Mapped[str] = mapped_column(String(8))


class Order(Base):
    """total is a snapshot already net of discount (SUM(order_items.total) - discount).
    The application must recalculate it whenever items change — never edited by hand.

    client_reference/occurred_at are only ever set by the mobile sync push (Fase 5):
    client_reference is the idempotency key (a UUID the app generates offline) that lets
    a resent batch not create a duplicate sale; occurred_at is when the sale actually
    happened offline, as opposed to created_at (when the row was written/synced). Both
    stay NULL for orders created through the regular online endpoint, where created_at
    already IS the occurrence time."""

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("total >= 0", name="ck_orders_total"),
        CheckConstraint("discount >= 0", name="ck_orders_discount"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("people.id"))  # a person with the "client" role
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    discount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    payment_method: Mapped[PaymentMethod] = mapped_column(pg_enum(PaymentMethod, "payment_method"))
    status: Mapped[OrderStatus] = mapped_column(
        pg_enum(OrderStatus, "order_status"), default=OrderStatus.PENDING
    )
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    filial_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    client_reference: Mapped[uuid.UUID | None] = mapped_column(Uuid, unique=True, default=None)
    occurred_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class OrderItem(Base):
    """quantity = actually fulfilled. original_qty is filled in only during mobile sync
    (Fase 5) when server-side stock is less than what was requested offline — it stays
    NULL in the normal case. cost_at_sale freezes product_costs.cost_value at sale time,
    the same way unit_price freezes the price — needed for correct margin analysis even
    if the product's cost changes later.

    discount_allocated is this item's share of orders.discount (proportional to the item's
    gross value, rounding remainder on the largest item — migration 0009). total stays the
    GROSS line value; net_total is the official revenue of the line, and for every order
    SUM(net_total) = orders.total."""

    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity"),
        CheckConstraint(
            "original_qty IS NULL OR original_qty >= quantity", name="ck_order_items_original_qty"
        ),
        CheckConstraint("unit_price >= 0", name="ck_order_items_unit_price"),
        CheckConstraint("cost_at_sale >= 0", name="ck_order_items_cost_at_sale"),
        CheckConstraint(
            "discount_allocated >= 0 AND discount_allocated <= quantity * unit_price",
            name="ck_order_items_discount_allocated",
        ),
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
    discount_allocated: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    net_total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), Computed("quantity * unit_price - discount_allocated", persisted=True)
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
    # set for a received purchase (reason 'purchase') or its cancellation — migration 0015
    purchase_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchase_items.id", ondelete="SET NULL"), default=None
    )
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
