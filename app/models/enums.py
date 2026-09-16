import enum

from sqlalchemy import Enum as PgEnum


class RecordStatus(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    CANCELLED = "cancelled"


class PaymentMethod(str, enum.Enum):
    CASH = "cash"
    DEBIT_CARD = "debit_card"
    CREDIT_CARD = "credit_card"
    PIX = "pix"
    OTHER = "other"


class StockMovementReason(str, enum.Enum):
    SALE = "sale"
    ADJUSTMENT = "adjustment"
    LOSS = "loss"
    RETURN = "return"
    RESTOCK = "restock"


def pg_enum(enum_cls: type[enum.Enum], name: str) -> PgEnum:
    """Maps a Python enum to the matching Postgres native enum type.

    The type itself is created by the Fase 0 migration's raw SQL (CREATE TYPE), not by
    SQLAlchemy — hence create_type=False. values_callable is required because SQLAlchemy's
    default Enum type persists the member *name* (e.g. "ACTIVE"), but our Postgres enum
    labels are lowercase (e.g. 'active') to match the DDL.
    """
    return PgEnum(
        enum_cls,
        name=name,
        values_callable=lambda members: [member.value for member in members],
        native_enum=True,
        create_type=False,
    )
