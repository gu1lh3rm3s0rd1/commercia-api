from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Product, ProductCost
from app.models.enums import OrderStatus, RecordStatus, StockMovementReason
from app.core.people import CLIENT
from app.models.sales import Order, OrderItem, Person, StockMovement
from app.core.timezone import local_day_bounds, local_today, to_local
from app.modules.catalog.service import get_price_at, lock_or_create_stock, lock_stocks
from app.modules.sales.schemas import OrderCreate

# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


def _get_client_in_filial(db: Session, client_id: int, filial_id: int) -> Person:
    client = db.get(Person, client_id)
    # a supplier that isn't also a client can't be the buyer of a sale
    if client is None or client.filial_id != filial_id or CLIENT not in client.roles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Client not found in this filial"
        )
    if client.status != RecordStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Client {client_id} is inactive"
        )
    return client


def _get_sellable_product(db: Session, product_id: int, filial_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None or product.filial_id != filial_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Product {product_id} not found in this filial",
        )
    if product.status != RecordStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Product {product_id} is not active"
        )
    return product


def get_order_or_404(db: Session, order_id: int, filial_ids: set[int]) -> Order:
    order = db.get(Order, order_id)
    if order is None or order.filial_id not in filial_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return order


def get_order_items(db: Session, order_id: int) -> list[OrderItem]:
    return list(db.execute(select(OrderItem).where(OrderItem.order_id == order_id)).scalars().all())


def get_order_with_items(db: Session, order_id: int, filial_ids: set[int]) -> tuple[Order, list[OrderItem]]:
    order = get_order_or_404(db, order_id, filial_ids)
    return order, get_order_items(db, order_id)


def get_order_items_bulk(db: Session, order_ids: list[int]) -> dict[int, list[OrderItem]]:
    """Items of a whole page of orders in one query (was one query per order)."""
    result: dict[int, list[OrderItem]] = {oid: [] for oid in order_ids}
    if order_ids:
        rows = db.execute(
            select(OrderItem).where(OrderItem.order_id.in_(order_ids)).order_by(OrderItem.id)
        ).scalars().all()
        for item in rows:
            result[item.order_id].append(item)
    return result


def list_orders(
    db: Session,
    filial_ids: set[int],
    status_filter: OrderStatus | None,
    limit: int,
    offset: int,
    client_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> tuple[list[Order], int]:
    """start_date/end_date: business-timezone days (inclusive) of when the sale really
    happened — occurred_at for offline sales, created_at otherwise (same rule as reports)."""
    base = select(Order).where(Order.filial_id.in_(filial_ids))
    if status_filter is not None:
        base = base.where(Order.status == status_filter)
    if client_id is not None:
        base = base.where(Order.client_id == client_id)
    effective = func.coalesce(Order.occurred_at, Order.created_at)
    if start_date is not None:
        base = base.where(effective >= local_day_bounds(start_date)[0])
    if end_date is not None:
        base = base.where(effective < local_day_bounds(end_date)[1])
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    items = db.execute(base.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit).offset(offset)).scalars().all()
    return list(items), total


# ---------------------------------------------------------------------
# create — the core of Fase 4: price lookup, stock check, totals, historization
# ---------------------------------------------------------------------


def allocate_discount(discount: Decimal, gross_lines: list[Decimal]) -> list[Decimal]:
    """Splits the order discount across its lines in proportion to each line's gross value,
    rounded to cents; the rounding remainder goes to the largest line (first one on ties) so
    the shares always add up to exactly `discount`. Same rule as migration 0009's backfill."""
    order_gross = sum(gross_lines, Decimal("0"))
    if discount == 0 or order_gross == 0:
        return [Decimal("0.00")] * len(gross_lines)
    shares = [(discount * gross / order_gross).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for gross in gross_lines]
    largest = max(range(len(gross_lines)), key=lambda i: (gross_lines[i], -i))
    shares[largest] += discount - sum(shares, Decimal("0"))
    return shares


def create_order(
    db: Session,
    filial_id: int,
    created_by_id: int,
    data: OrderCreate,
    client_reference: UUID | None = None,
    occurred_at: datetime | None = None,
) -> tuple[Order, list[OrderItem]]:
    """client_reference/occurred_at are only passed by the mobile sync push (offline sales).

    Concurrency: the stock rows of every product in the order are locked (FOR UPDATE)
    before availability is checked and kept locked until the commit, so two simultaneous
    sales can't both sell the last units nor overwrite each other's stock update."""
    client = _get_client_in_filial(db, data.client_id, filial_id)
    # An offline sale is priced as of the day it happened (business timezone), not the day
    # it was synced — the price the customer actually paid.
    sale_day = to_local(occurred_at).date() if occurred_at is not None else local_today()

    # Pass 1 — validate everything and compute what each line needs, WITHOUT writing
    # anything yet. If any item fails, we bail out before touching the DB at all.
    products: dict[int, tuple[Product, Decimal, Decimal]] = {}  # id -> product, unit price, cost
    for line in data.items:
        if line.product_id in products:
            continue
        product = _get_sellable_product(db, line.product_id, filial_id)
        price = get_price_at(db, product.id, sale_day)
        if price is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product {product.id} has no price set for {sale_day}",
            )
        # price_offer == 0 means "no offer" (team convention: never null) — sell at the
        # offer only when there is a real one, otherwise a missing offer would sell for free.
        unit_price = price.price_offer if price.price_offer > 0 else price.price_default
        cost_row = db.execute(
            select(ProductCost).where(ProductCost.product_id == product.id)
        ).scalar_one_or_none()
        cost_at_sale = cost_row.cost_value if cost_row is not None else Decimal("0")
        products[product.id] = (product, unit_price, cost_at_sale)

    stocks = lock_stocks(db, filial_id, products)
    remaining = {pid: (stocks[pid].quantity if pid in stocks else 0) for pid in products}
    requested: dict[int, int] = {}
    for line in data.items:
        requested[line.product_id] = requested.get(line.product_id, 0) + line.quantity
    for product_id, quantity in requested.items():
        # Summed per product: the same product on two lines must fit in the stock together.
        if remaining[product_id] < quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient stock for product {product_id}: available {remaining[product_id]}, "
                f"requested {quantity}",
            )

    subtotal = sum((products[line.product_id][1] * line.quantity for line in data.items), Decimal("0"))
    if data.discount > subtotal:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Discount cannot exceed the order subtotal"
        )
    total = subtotal - data.discount
    allocation = allocate_discount(
        data.discount, [products[line.product_id][1] * line.quantity for line in data.items]
    )

    # Pass 2 — everything validated, now actually write order + items + stock + movements
    # as one transaction (single commit at the end). Lines are kept as sent; each line's
    # movement records the running stock after it.
    order = Order(
        client_id=client.id,
        total=total,
        discount=data.discount,
        payment_method=data.payment_method,
        status=OrderStatus.PENDING,
        created_by_id=created_by_id,
        filial_id=filial_id,
        client_reference=client_reference,
        occurred_at=occurred_at,
    )
    db.add(order)
    db.flush()  # need order.id below

    order_items: list[OrderItem] = []
    for line, discount_share in zip(data.items, allocation):
        product, unit_price, cost_at_sale = products[line.product_id]
        order_item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=line.quantity,
            unit_price=unit_price,
            cost_at_sale=cost_at_sale,
            discount_allocated=discount_share,
        )
        db.add(order_item)
        db.flush()  # need order_item.id for the stock_movements row below

        remaining[product.id] -= line.quantity
        stocks[product.id].quantity = remaining[product.id]
        db.add(
            StockMovement(
                product_id=product.id,
                filial_id=filial_id,
                change_qty=-line.quantity,
                reason=StockMovementReason.SALE,
                resulting_qty=remaining[product.id],
                order_item_id=order_item.id,
            )
        )
        order_items.append(order_item)

    db.commit()
    db.refresh(order)
    for item in order_items:
        db.refresh(item)
    return order, order_items


# ---------------------------------------------------------------------
# status transitions (UC013)
# ---------------------------------------------------------------------


def _lock_order(db: Session, order: Order) -> Order:
    """Re-reads the order with FOR UPDATE: the status checks below see the committed state,
    and a concurrent pay/cancel of the same order waits instead of racing (cancelling twice
    at the same time would return the stock twice)."""
    return db.execute(
        select(Order).where(Order.id == order.id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()


def mark_as_paid(db: Session, order: Order) -> Order:
    order = _lock_order(db, order)
    if order.status != OrderStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Only a pending order can be marked as paid"
        )
    order.status = OrderStatus.PAID
    db.commit()
    db.refresh(order)
    return order


def cancel_order(db: Session, order: Order) -> Order:
    order = _lock_order(db, order)
    if order.status == OrderStatus.CANCELLED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Order is already cancelled")

    # Reverse every item's stock decrement with a new movement — never edit the original
    # SALE movement, never a raw UPDATE to stocks.quantity without a movement row.
    items = get_order_items(db, order.id)
    stocks = lock_stocks(db, order.filial_id, [item.product_id for item in items])
    for item in items:
        stock_row = stocks.get(item.product_id)
        if stock_row is None:
            stock_row = stocks[item.product_id] = lock_or_create_stock(db, item.product_id, order.filial_id)
        stock_row.quantity += item.quantity
        db.add(
            StockMovement(
                product_id=item.product_id,
                filial_id=order.filial_id,
                change_qty=item.quantity,
                reason=StockMovementReason.ADJUSTMENT,
                resulting_qty=stock_row.quantity,
                order_item_id=item.id,
            )
        )

    order.status = OrderStatus.CANCELLED
    db.commit()
    db.refresh(order)
    return order
