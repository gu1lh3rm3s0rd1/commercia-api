from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.catalog import Category, Department, Price, Product, Stock
from app.models.sales import Order
from app.modules.catalog.schemas import CategoryRead, DepartmentRead, PriceRead, ProductRead, StockRead
from app.modules.sales import service as sales_service
from app.modules.sales.schemas import OrderCreate
from app.modules.sync.schemas import PullResponse, PushResultItem, SyncSaleCreate

# ---------------------------------------------------------------------
# push
# ---------------------------------------------------------------------


def _find_order_by_reference(db: Session, client_reference) -> Order | None:
    return db.execute(select(Order).where(Order.client_reference == client_reference)).scalar_one_or_none()


def push_sale(
    db: Session, filial_id: int, created_by_id: int, sale: SyncSaleCreate
) -> PushResultItem:
    """Each sale in the batch is independent — one rejection never blocks the rest.
    Reuses sales_service.create_order as-is (same validate-everything-then-write-once
    logic from Fase 4): if stock is no longer available by sync time, the whole sale is
    rejected (no partial fulfillment — original_qty is intentionally not used here,
    per the 2026-09-25 decision)."""
    existing = _find_order_by_reference(db, sale.client_reference)
    if existing is not None:
        return PushResultItem(
            client_reference=sale.client_reference, status="duplicate", order_id=existing.id
        )

    order_data = OrderCreate(
        client_id=sale.client_id,
        payment_method=sale.payment_method,
        discount=sale.discount,
        items=sale.items,
        client_reference=sale.client_reference,
        occurred_at=sale.occurred_at,
    )
    try:
        order, _items = sales_service.create_order(db, filial_id, created_by_id, order_data)
    except HTTPException as exc:
        db.rollback()
        return PushResultItem(
            client_reference=sale.client_reference, status="rejected", reason=str(exc.detail)
        )
    except IntegrityError:
        # Race: another request already inserted this client_reference between our
        # check above and the insert (the UNIQUE constraint is the real guarantee).
        db.rollback()
        existing = _find_order_by_reference(db, sale.client_reference)
        if existing is not None:
            return PushResultItem(
                client_reference=sale.client_reference, status="duplicate", order_id=existing.id
            )
        raise

    return PushResultItem(client_reference=sale.client_reference, status="created", order_id=order.id)


def push_batch(
    db: Session, filial_id: int, created_by_id: int, sales: list[SyncSaleCreate]
) -> list[PushResultItem]:
    return [push_sale(db, filial_id, created_by_id, sale) for sale in sales]


# ---------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------


def pull_changes(db: Session, filial_id: int, last_synced_at: datetime | None) -> PullResponse:
    # Captured before running the queries below: a row that commits in the tiny window
    # between this line and the queries just gets picked up on the *next* pull (its
    # updated_at will be > this snapshot) — nothing is ever lost, only delayed by one
    # cycle in the rarest case. Standard incremental-sync tradeoff.
    snapshot_time = datetime.now(timezone.utc)

    departments = db.execute(select(Department).order_by(Department.name)).scalars().all()

    categories_stmt = select(Category).where(Category.company_id == filial_id)
    if last_synced_at is not None:
        categories_stmt = categories_stmt.where(Category.updated_at > last_synced_at)
    categories = db.execute(categories_stmt).scalars().all()

    products_stmt = select(Product).where(Product.filial_id == filial_id)
    if last_synced_at is not None:
        products_stmt = products_stmt.where(Product.updated_at > last_synced_at)
    products = db.execute(products_stmt).scalars().all()

    # prices is an append-only log — a "change" is a new row, so created_at is the
    # right field to filter on (there's no updated_at, rows are never updated).
    prices_stmt = select(Price).where(Price.filial_id == filial_id)
    if last_synced_at is not None:
        prices_stmt = prices_stmt.where(Price.created_at > last_synced_at)
    prices = db.execute(prices_stmt).scalars().all()

    stocks_stmt = select(Stock).where(Stock.filial_id == filial_id)
    if last_synced_at is not None:
        stocks_stmt = stocks_stmt.where(Stock.updated_at > last_synced_at)
    stocks = db.execute(stocks_stmt).scalars().all()

    return PullResponse(
        synced_at=snapshot_time,
        departments=[DepartmentRead.model_validate(d) for d in departments],
        categories=[CategoryRead.model_validate(c) for c in categories],
        products=[ProductRead.model_validate(p) for p in products],
        prices=[PriceRead.model_validate(p) for p in prices],
        stocks=[StockRead.model_validate(s) for s in stocks],
    )
