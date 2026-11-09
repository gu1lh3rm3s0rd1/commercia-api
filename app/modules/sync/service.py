from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import or_, select, union
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import audit
from app.core.timezone import local_today, to_local
from app.models.catalog import Category, Department, Price, Product, ProductCost, Stock
from app.models.sales import Order
from app.models.user import User, UserCompany
from app.modules.catalog import service as catalog_service
from app.modules.catalog.schemas import CategoryRead, DepartmentRead, PriceRead, StockRead
from app.modules.sales import service as sales_service
from app.modules.sales.schemas import OrderCreate
from app.modules.sync.schemas import PullResponse, PushResultItem, SyncSaleCreate, SyncUserRead

# ---------------------------------------------------------------------
# push
# ---------------------------------------------------------------------


# Overlap applied to last_synced_at: a row written by a transaction that started before the
# previous pull's snapshot but committed after it has an older timestamp than the snapshot,
# so a strict "> last_synced_at" filter would skip it forever. Re-sending a few minutes of
# rows is harmless — the app upserts by id.
PULL_OVERLAP = timedelta(minutes=5)


def _find_order_by_reference(db: Session, client_reference) -> Order | None:
    return db.execute(select(Order).where(Order.client_reference == client_reference)).scalar_one_or_none()


def _same_sale(db: Session, order: Order, sale: SyncSaleCreate) -> bool:
    """Whether an already-synced order is the same sale being re-sent (a retry) or a
    different sale reusing the reference. Compares what the app sends, never prices."""
    items = sorted((i.product_id, i.quantity) for i in sales_service.get_order_items(db, order.id))
    return (
        order.client_id == sale.client_id
        and order.payment_method == sale.payment_method
        and order.discount == sale.discount
        and order.occurred_at == sale.occurred_at
        and items == sorted((i.product_id, i.quantity) for i in sale.items)
    )


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
        return _existing_result(db, existing, filial_id, sale)

    order_data = OrderCreate(
        client_id=sale.client_id,
        payment_method=sale.payment_method,
        discount=sale.discount,
        items=sale.items,
    )
    try:
        order, _items = sales_service.create_order(
            db, filial_id, created_by_id, order_data,
            client_reference=sale.client_reference, occurred_at=sale.occurred_at,
        )
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
            return _existing_result(db, existing, filial_id, sale)
        raise

    return PushResultItem(client_reference=sale.client_reference, status="created", order_id=order.id)


def _existing_result(db: Session, existing: Order, filial_id: int, sale: SyncSaleCreate) -> PushResultItem:
    if existing.filial_id != filial_id:
        # another store's order: never reveal its id
        return PushResultItem(client_reference=sale.client_reference, status="conflict",
                              reason="client_reference already used by another filial")
    if not _same_sale(db, existing, sale):
        return PushResultItem(client_reference=sale.client_reference, status="conflict", order_id=existing.id,
                              reason="client_reference already used by a different sale")
    return PushResultItem(client_reference=sale.client_reference, status="duplicate", order_id=existing.id)


def push_batch(
    db: Session, filial_id: int, created_by_id: int, sales: list[SyncSaleCreate]
) -> list[PushResultItem]:
    audit.set_source(db, "sync")  # the orders below are logged as offline sales
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

    since = last_synced_at - PULL_OVERLAP if last_synced_at is not None else None

    categories_stmt = select(Category).where(Category.company_id == filial_id)
    if since is not None:
        categories_stmt = categories_stmt.where(Category.updated_at > since)
    categories = db.execute(categories_stmt).scalars().all()

    # Each product embeds its current price, cost and stock, so it must be re-sent when ANY
    # of those changed — not only when the product row itself did. That includes a price
    # whose validity started or ended by date since the last pull (no row is written then).
    products_stmt = select(Product).where(Product.filial_id == filial_id)
    if since is not None:
        since_day, today = to_local(since).date(), local_today()
        changed = union(
            select(Product.id).where(Product.filial_id == filial_id, Product.updated_at > since),
            select(Price.product_id).where(Price.filial_id == filial_id, Price.created_at > since),
            select(Price.product_id).where(
                Price.filial_id == filial_id,
                or_(Price.start_date.between(since_day, today), Price.end_date.between(since_day - timedelta(days=1), today)),
            ),
            select(ProductCost.product_id).where(ProductCost.updated_at > since),
            select(Stock.product_id).where(Stock.filial_id == filial_id, Stock.updated_at > since),
        )
        products_stmt = products_stmt.where(Product.id.in_(changed))
    products = db.execute(products_stmt).scalars().all()
    product_ids = [p.id for p in products]
    current_prices = catalog_service.get_current_prices_bulk(db, product_ids)
    product_costs = catalog_service.get_product_costs_bulk(db, product_ids)
    product_stocks = catalog_service.get_stocks_bulk(db, product_ids)

    # prices is an append-only log — a "change" is a new row, so created_at is the
    # right field to filter on (there's no updated_at, rows are never updated).
    prices_stmt = select(Price).where(Price.filial_id == filial_id)
    if since is not None:
        prices_stmt = prices_stmt.where(Price.created_at > since)
    prices = db.execute(prices_stmt).scalars().all()

    stocks_stmt = select(Stock).where(Stock.filial_id == filial_id)
    if since is not None:
        stocks_stmt = stocks_stmt.where(Stock.updated_at > since)
    stocks = db.execute(stocks_stmt).scalars().all()

    # Same rule as get_accessible_filial_ids: a superuser reaches every filial without a
    # user_companies row, so they must be able to log in offline on any store's device.
    users_stmt = (
        select(User)
        .where(
            or_(
                User.is_superuser.is_(True),
                User.id.in_(select(UserCompany.user_id).where(UserCompany.filial_id == filial_id)),
            )
        )
        .order_by(User.id)
    )
    users = db.execute(users_stmt).scalars().all()

    return PullResponse(
        synced_at=snapshot_time,
        departments=[DepartmentRead.model_validate(d) for d in departments],
        categories=[CategoryRead.model_validate(c) for c in categories],
        products=[
            catalog_service.to_product_read(
                p, current_prices.get(p.id), product_costs.get(p.id), product_stocks.get(p.id)
            )
            for p in products
        ],
        prices=[PriceRead.model_validate(p) for p in prices],
        stocks=[StockRead.model_validate(s) for s in stocks],
        users=[SyncUserRead.model_validate(u) for u in users],
    )
