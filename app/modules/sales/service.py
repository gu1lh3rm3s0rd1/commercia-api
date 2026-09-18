from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Product, ProductCost, Stock
from app.models.enums import OrderStatus, RecordStatus, StockMovementReason
from app.models.sales import Client, Order, OrderItem, StockMovement
from app.modules.catalog.service import get_current_price
from app.modules.sales.schemas import OrderCreate

# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


def _get_client_in_filial(db: Session, client_id: int, filial_id: int) -> Client:
    client = db.get(Client, client_id)
    if client is None or client.filial_id != filial_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Client not found in this filial"
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


def _get_stock_row(db: Session, product_id: int, filial_id: int) -> Stock | None:
    return db.execute(
        select(Stock).where(Stock.product_id == product_id, Stock.filial_id == filial_id)
    ).scalar_one_or_none()


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


def list_orders(
    db: Session, filial_ids: set[int], status_filter: OrderStatus | None, limit: int, offset: int
) -> tuple[list[Order], int]:
    base = select(Order).where(Order.filial_id.in_(filial_ids))
    if status_filter is not None:
        base = base.where(Order.status == status_filter)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    items = db.execute(base.order_by(Order.created_at.desc()).limit(limit).offset(offset)).scalars().all()
    return list(items), total


# ---------------------------------------------------------------------
# create — the core of Fase 4: price lookup, stock check, totals, historization
# ---------------------------------------------------------------------


def create_order(
    db: Session, filial_id: int, created_by_id: int, data: OrderCreate
) -> tuple[Order, list[OrderItem]]:
    client = _get_client_in_filial(db, data.client_id, filial_id)

    # Pass 1 — validate everything and compute what each line needs, WITHOUT writing
    # anything yet. If any item fails, we bail out before touching the DB at all.
    prepared: list[tuple[Product, int, Decimal, Decimal, Stock | None, int]] = []
    for line in data.items:
        product = _get_sellable_product(db, line.product_id, filial_id)

        price = get_current_price(db, product.id)
        if price is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product {product.id} has no price set for today",
            )
        unit_price = price.price_offer if price.price_offer is not None else price.price_default

        cost_row = db.execute(
            select(ProductCost).where(ProductCost.product_id == product.id)
        ).scalar_one_or_none()
        cost_at_sale = cost_row.cost_value if cost_row is not None else Decimal("0")

        stock_row = _get_stock_row(db, product.id, filial_id)
        available = stock_row.quantity if stock_row is not None else 0
        if available < line.quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient stock for product {product.id}: available {available}, "
                f"requested {line.quantity}",
            )

        prepared.append((product, line.quantity, unit_price, cost_at_sale, stock_row, available - line.quantity))

    subtotal = sum((qty * unit_price for _, qty, unit_price, _, _, _ in prepared), Decimal("0"))
    if data.discount > subtotal:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Discount cannot exceed the order subtotal"
        )
    total = subtotal - data.discount

    # Pass 2 — everything validated, now actually write order + items + stock + movements
    # as one transaction (single commit at the end).
    order = Order(
        client_id=client.id,
        total=total,
        discount=data.discount,
        payment_method=data.payment_method,
        status=OrderStatus.PENDING,
        created_by_id=created_by_id,
        filial_id=filial_id,
    )
    db.add(order)
    db.flush()  # need order.id below

    order_items: list[OrderItem] = []
    for product, quantity, unit_price, cost_at_sale, stock_row, new_qty in prepared:
        order_item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=quantity,
            unit_price=unit_price,
            cost_at_sale=cost_at_sale,
        )
        db.add(order_item)
        db.flush()  # need order_item.id for the stock_movements row below

        if stock_row is None:
            stock_row = Stock(product_id=product.id, filial_id=filial_id, quantity=new_qty)
            db.add(stock_row)
        else:
            stock_row.quantity = new_qty

        db.add(
            StockMovement(
                product_id=product.id,
                filial_id=filial_id,
                change_qty=-quantity,
                reason=StockMovementReason.SALE,
                resulting_qty=new_qty,
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


def mark_as_paid(db: Session, order: Order) -> Order:
    if order.status != OrderStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Only a pending order can be marked as paid"
        )
    order.status = OrderStatus.PAID
    db.commit()
    db.refresh(order)
    return order


def cancel_order(db: Session, order: Order) -> Order:
    if order.status == OrderStatus.CANCELLED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Order is already cancelled")

    # Reverse every item's stock decrement with a new movement — never edit the original
    # SALE movement, never a raw UPDATE to stocks.quantity without a movement row.
    for item in get_order_items(db, order.id):
        stock_row = _get_stock_row(db, item.product_id, order.filial_id)
        current_qty = stock_row.quantity if stock_row is not None else 0
        new_qty = current_qty + item.quantity

        if stock_row is None:
            stock_row = Stock(product_id=item.product_id, filial_id=order.filial_id, quantity=new_qty)
            db.add(stock_row)
        else:
            stock_row.quantity = new_qty

        db.add(
            StockMovement(
                product_id=item.product_id,
                filial_id=order.filial_id,
                change_qty=item.quantity,
                reason=StockMovementReason.ADJUSTMENT,
                resulting_qty=new_qty,
                order_item_id=item.id,
            )
        )

    order.status = OrderStatus.CANCELLED
    db.commit()
    db.refresh(order)
    return order
