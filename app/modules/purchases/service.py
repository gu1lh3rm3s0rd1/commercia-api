"""Purchases (Etapa 3). Same discipline as sales: validate everything first, then write
purchase + items (+ stock + movements + cost, when received) in ONE commit, with the stock
rows locked so concurrent sales/receipts can't overwrite each other.

Cost rule (decided 2026-11-14): the product's cost becomes the landed unit cost of its LAST
purchase — last by when the purchase happened (occurred_at for offline ones, received_at
otherwise), compared with product_costs.effective_at. An old offline purchase that syncs
late adds its stock but doesn't overwrite a newer cost. Cancelling a received purchase
takes the stock back out but leaves the cost as it is (there's no "previous cost" to go
back to — set it manually if needed)."""

from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.people import SUPPLIER
from app.core.timezone import local_day_bounds
from app.models.catalog import Product, ProductCost
from app.models.enums import PurchaseStatus, RecordStatus, StockMovementReason
from app.models.purchases import Purchase, PurchaseItem
from app.models.sales import Person, StockMovement
from app.modules.catalog.service import lock_or_create_stock, lock_stocks
from app.modules.purchases.schemas import PurchaseCreate
from app.modules.sales.service import allocate_discount  # same proportional split, reused for freight

CENT = Decimal("0.01")


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _get_supplier_in_filial(db: Session, supplier_id: int, filial_id: int) -> Person:
    supplier = db.get(Person, supplier_id)
    if supplier is None or supplier.filial_id != filial_id or SUPPLIER not in supplier.roles:
        raise _bad_request("Supplier not found in this filial")
    if supplier.status != RecordStatus.ACTIVE:
        raise _bad_request(f"Supplier {supplier_id} is inactive")
    return supplier


def _get_product_in_filial(db: Session, product_id: int, filial_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None or product.filial_id != filial_id:
        raise _bad_request(f"Product {product_id} not found in this filial")
    if product.status != RecordStatus.ACTIVE:
        raise _bad_request(f"Product {product_id} is inactive")
    return product


# ---------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------


def get_purchase_or_404(db: Session, purchase_id: int, filial_ids: set[int]) -> Purchase:
    purchase = db.get(Purchase, purchase_id)
    if purchase is None or purchase.filial_id not in filial_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")
    return purchase


def get_items(db: Session, purchase_id: int) -> list[PurchaseItem]:
    return list(db.execute(select(PurchaseItem).where(PurchaseItem.purchase_id == purchase_id)
                           .order_by(PurchaseItem.id)).scalars().all())


def get_items_bulk(db: Session, purchase_ids: list[int]) -> dict[int, list[PurchaseItem]]:
    result: dict[int, list[PurchaseItem]] = {pid: [] for pid in purchase_ids}
    if purchase_ids:
        for item in db.execute(select(PurchaseItem).where(PurchaseItem.purchase_id.in_(purchase_ids))
                               .order_by(PurchaseItem.id)).scalars().all():
            result[item.purchase_id].append(item)
    return result


def list_purchases(db: Session, filial_ids: set[int], supplier_id: int | None, status_filter: PurchaseStatus | None,
                   start_date: date | None, end_date: date | None, limit: int, offset: int) -> tuple[list[Purchase], int]:
    """Period = business-timezone days of when the purchase happened (occurred_at offline,
    created_at otherwise)."""
    base = select(Purchase).where(Purchase.filial_id.in_(filial_ids))
    if supplier_id is not None:
        base = base.where(Purchase.supplier_id == supplier_id)
    if status_filter is not None:
        base = base.where(Purchase.status == status_filter)
    effective = func.coalesce(Purchase.occurred_at, Purchase.created_at)
    if start_date is not None:
        base = base.where(effective >= local_day_bounds(start_date)[0])
    if end_date is not None:
        base = base.where(effective < local_day_bounds(end_date)[1])
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(base.order_by(effective.desc(), Purchase.id.desc()).limit(limit).offset(offset)).scalars().all()
    return list(rows), total


# ---------------------------------------------------------------------
# create / receive / cancel
# ---------------------------------------------------------------------


def create_purchase(db: Session, filial_id: int, user_id: int, data: PurchaseCreate,
                    client_reference: UUID | None = None, occurred_at: datetime | None = None,
                    ) -> tuple[Purchase, list[PurchaseItem]]:
    """client_reference/occurred_at only come from the mobile sync (which always receives)."""
    # Pass 1 — validate everything, write nothing.
    _get_supplier_in_filial(db, data.supplier_id, filial_id)
    for product_id in {line.product_id for line in data.items}:
        _get_product_in_filial(db, product_id, filial_id)
    gross = [line.unit_cost * line.quantity for line in data.items]
    subtotal = sum(gross, Decimal("0"))
    if data.discount > subtotal:
        raise _bad_request("Discount cannot exceed the purchase subtotal")
    discounts = allocate_discount(data.discount, gross)
    freights = allocate_discount(data.freight, gross)
    if data.freight > 0 and subtotal == 0:  # nothing to split it over
        raise _bad_request("Freight needs at least one item with a cost")

    # Pass 2 — write (single commit).
    purchase = Purchase(
        supplier_id=data.supplier_id, filial_id=filial_id, status=PurchaseStatus.PENDING,
        total=subtotal - data.discount + data.freight, discount=data.discount, freight=data.freight,
        invoice_number=data.invoice_number, invoice_key=data.invoice_key, issued_at=data.issued_at, notes=data.notes,
        client_reference=client_reference, occurred_at=occurred_at, created_by_id=user_id,
    )
    db.add(purchase)
    db.flush()
    items = []
    for line, discount_share, freight_share in zip(data.items, discounts, freights):
        item = PurchaseItem(purchase_id=purchase.id, product_id=line.product_id, quantity=line.quantity,
                            unit_cost=line.unit_cost, discount_allocated=discount_share, freight_allocated=freight_share)
        db.add(item)
        items.append(item)
    db.flush()
    if data.receive:
        _receive(db, purchase, items, user_id)
    db.commit()
    db.refresh(purchase)
    for item in items:
        db.refresh(item)
    return purchase, items


def _lock(db: Session, purchase: Purchase) -> Purchase:
    return db.execute(select(Purchase).where(Purchase.id == purchase.id).with_for_update()
                      .execution_options(populate_existing=True)).scalar_one()


def _landed(item: PurchaseItem) -> Decimal:
    return (item.quantity * item.unit_cost - item.discount_allocated + item.freight_allocated) / item.quantity


def _receive(db: Session, purchase: Purchase, items: list[PurchaseItem], user_id: int) -> None:
    """Stock in (one 'purchase' movement per item) + last-cost rule. Caller commits."""
    now = datetime.now(timezone.utc)
    product_ids = sorted({i.product_id for i in items})
    stocks = lock_stocks(db, purchase.filial_id, product_ids)
    for item in items:
        stock = stocks.get(item.product_id) or lock_or_create_stock(db, item.product_id, purchase.filial_id)
        stocks[item.product_id] = stock
        stock.quantity += item.quantity
        db.add(StockMovement(product_id=item.product_id, filial_id=purchase.filial_id, change_qty=item.quantity,
                             reason=StockMovementReason.PURCHASE, resulting_qty=stock.quantity,
                             purchase_item_id=item.id))

    happened_at = purchase.occurred_at or now
    for product_id in product_ids:
        lines = [i for i in items if i.product_id == product_id]
        quantity = sum(i.quantity for i in lines)
        landed = (sum((_landed(i) * i.quantity for i in lines), Decimal("0")) / quantity).quantize(CENT, ROUND_HALF_UP)
        cost = db.execute(select(ProductCost).where(ProductCost.product_id == product_id).with_for_update()).scalar_one_or_none()
        if cost is None:
            db.add(ProductCost(product_id=product_id, cost_value=landed, effective_at=happened_at))
        elif cost.effective_at is None or happened_at >= cost.effective_at:
            cost.cost_value = landed
            cost.effective_at = happened_at
        # else: a newer cost is already in place (this purchase happened before it)

    purchase.status = PurchaseStatus.RECEIVED
    purchase.received_at = now
    purchase.received_by_id = user_id


def receive_purchase(db: Session, purchase: Purchase, user_id: int) -> Purchase:
    purchase = _lock(db, purchase)
    if purchase.status != PurchaseStatus.PENDING:
        raise _bad_request("Only a pending purchase can be received")
    _get_supplier_in_filial(db, purchase.supplier_id, purchase.filial_id)  # still an active supplier?
    _receive(db, purchase, get_items(db, purchase.id), user_id)
    db.commit()
    db.refresh(purchase)
    return purchase


def cancel_purchase(db: Session, purchase: Purchase) -> Purchase:
    """Pending → just cancelled. Received → its stock goes back out ('adjustment' movements
    linked to the items); refused if part of it was already sold (stock would go negative)."""
    purchase = _lock(db, purchase)
    if purchase.status == PurchaseStatus.CANCELLED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Purchase is already cancelled")
    if purchase.status == PurchaseStatus.RECEIVED:
        items = get_items(db, purchase.id)
        stocks = lock_stocks(db, purchase.filial_id, sorted({i.product_id for i in items}))
        needed: dict[int, int] = {}
        for item in items:
            needed[item.product_id] = needed.get(item.product_id, 0) + item.quantity
        for product_id, quantity in needed.items():
            available = stocks[product_id].quantity if product_id in stocks else 0
            if available < quantity:
                db.rollback()
                raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                    detail=f"Product {product_id}: only {available} in stock, the purchase added "
                                           f"{quantity} — it was already sold or adjusted")
        for item in items:
            stock = stocks[item.product_id]
            stock.quantity -= item.quantity
            db.add(StockMovement(product_id=item.product_id, filial_id=purchase.filial_id, change_qty=-item.quantity,
                                 reason=StockMovementReason.ADJUSTMENT, resulting_qty=stock.quantity,
                                 purchase_item_id=item.id))
    purchase.status = PurchaseStatus.CANCELLED
    db.commit()
    db.refresh(purchase)
    return purchase
