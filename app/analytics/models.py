"""Tables of the `analytics` schema (migration 0005) — derived from `public`, rebuilt by
app/analytics/jobs.py. Read-only for everyone else (Fase 8 forecasting reads these)."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SCHEMA = {"schema": "analytics"}


class DailySales(Base):
    """Sparse: a row only when the product sold that (business-timezone) day."""

    __tablename__ = "daily_sales"
    __table_args__ = _SCHEMA

    sale_date: Mapped[date] = mapped_column(primary_key=True)
    filial_id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(primary_key=True)
    orders_count: Mapped[int]
    quantity: Mapped[int]
    revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    cost: Mapped[Decimal] = mapped_column(Numeric(14, 2))


class DailyStockSnapshot(Base):
    """Dense: every day from the product's first stock movement up to yesterday."""

    __tablename__ = "daily_stock_snapshot"
    __table_args__ = _SCHEMA

    snapshot_date: Mapped[date] = mapped_column(primary_key=True)
    filial_id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(primary_key=True)
    end_of_day_qty: Mapped[int]
    min_qty: Mapped[int]
    was_out_of_stock: Mapped[bool]


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = _SCHEMA

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    from_date: Mapped[date | None]
    to_date: Mapped[date | None]
    rows_sales: Mapped[int | None]
    rows_stock: Mapped[int | None]
    last_order_id: Mapped[int | None]
    last_stock_movement_id: Mapped[int | None]
    error: Mapped[str | None]
