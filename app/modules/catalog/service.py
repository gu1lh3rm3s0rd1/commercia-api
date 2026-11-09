from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.timezone import local_today
from app.models.catalog import Category, Department, Price, Product, ProductCost, Stock
from app.models.enums import RecordStatus, StockMovementReason
from app.models.sales import StockMovement
from app.modules.catalog.schemas import (
    ZERO,
    ProductCostRead,
    ProductCreate,
    ProductRead,
    ProductUpdate,
    PriceRead,
    StockRead,
)

# ---------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------


def ensure_filial_access(filial_id: int, accessible_filial_ids: set[int]) -> None:
    if filial_id not in accessible_filial_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this filial")


def get_product_or_404(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return product


# ---------------------------------------------------------------------
# departments — shared reference list, list/create only (see Fase 2 plan)
# ---------------------------------------------------------------------


def list_departments(db: Session) -> list[Department]:
    return list(db.execute(select(Department).order_by(Department.name)).scalars().all())


def create_department(db: Session, name: str) -> Department:
    department = Department(name=name)
    db.add(department)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Department already exists"
        ) from exc
    db.refresh(department)
    return department


# ---------------------------------------------------------------------
# categories — per filial, no delete (no status column, would break on FK)
# ---------------------------------------------------------------------


def list_categories(
    db: Session, filial_ids: set[int], limit: int, offset: int
) -> tuple[list[Category], int]:
    base = select(Category).where(Category.company_id.in_(filial_ids))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    items = db.execute(base.order_by(Category.name, Category.id).limit(limit).offset(offset)).scalars().all()
    return list(items), total


def create_category(
    db: Session, filial_id: int, name: str, department_id: int, created_by_id: int
) -> Category:
    if db.get(Department, department_id) is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Department not found")

    existing = db.execute(
        select(Category).where(Category.company_id == filial_id, Category.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Category already exists in this filial"
        )

    category = Category(
        name=name, department_id=department_id, company_id=filial_id, created_by_id=created_by_id
    )
    db.add(category)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Category already exists in this filial"
        ) from exc
    db.refresh(category)
    return category


def get_category_or_404(db: Session, category_id: int, filial_ids: set[int]) -> Category:
    category = db.get(Category, category_id)
    if category is None or category.company_id not in filial_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    return category


def update_category(
    db: Session, category: Category, name: str | None, department_id: int | None
) -> Category:
    if department_id is not None:
        if db.get(Department, department_id) is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Department not found")
        category.department_id = department_id
    if name is not None:
        category.name = name

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Category already exists in this filial"
        ) from exc
    db.refresh(category)
    return category


# ---------------------------------------------------------------------
# products — per filial; "delete" = deactivate (status), never a hard DELETE
# ---------------------------------------------------------------------


def list_products(
    db: Session, filial_ids: set[int], limit: int, offset: int
) -> tuple[list[Product], int]:
    base = select(Product).where(Product.filial_id.in_(filial_ids))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    items = db.execute(base.order_by(Product.name, Product.id).limit(limit).offset(offset)).scalars().all()
    return list(items), total


def to_product_read(
    product: Product, price: Price | None, cost: ProductCost | None, stock: Stock | None
) -> ProductRead:
    """Builds the full ProductRead, embedding current price/cost/stock. stock always comes
    through (quantity=0 synthetic row if none exists yet) — same fallback GET .../stock
    already uses; price/cost follow the same idea — a zero-valued row when nothing has
    been set (team convention: price/cost values are 0, never null)."""
    return ProductRead(
        id=product.id,
        sku=product.sku,
        name=product.name,
        description=product.description,
        brand=product.brand,
        barcode=product.barcode,
        category_id=product.category_id,
        filial_id=product.filial_id,
        weight=product.weight,
        height=product.height,
        width=product.width,
        depth=product.depth,
        status=product.status,
        created_at=product.created_at,
        updated_at=product.updated_at,
        current_price=(
            PriceRead.model_validate(price)
            if price is not None
            else PriceRead(
                product_id=product.id,
                filial_id=product.filial_id,
                price_default=ZERO,
                price_offer=ZERO,
                start_date=None,
                end_date=None,
            )
        ),
        cost=(
            ProductCostRead.model_validate(cost)
            if cost is not None
            else ProductCostRead(product_id=product.id, cost_value=ZERO)
        ),
        stock=(
            StockRead.model_validate(stock)
            if stock is not None
            else StockRead(product_id=product.id, filial_id=product.filial_id, quantity=0)
        ),
    )


def _validate_category_for_filial(db: Session, category_id: int, filial_id: int) -> None:
    category = db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Category not found")
    if category.company_id != filial_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category does not belong to this filial",
        )


def create_product(db: Session, filial_id: int, created_by_id: int, data: ProductCreate) -> Product:
    _validate_category_for_filial(db, data.category_id, filial_id)

    product = Product(
        sku=data.sku,
        name=data.name,
        description=data.description,
        brand=data.brand,
        barcode=data.barcode,
        category_id=data.category_id,
        filial_id=filial_id,
        weight=data.weight,
        height=data.height,
        width=data.width,
        depth=data.depth,
        created_by_id=created_by_id,
    )
    db.add(product)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="SKU or barcode already used in this filial",
        ) from exc
    db.refresh(product)
    return product


def update_product(db: Session, product: Product, data: ProductUpdate) -> Product:
    if data.category_id is not None:
        _validate_category_for_filial(db, data.category_id, product.filial_id)
        product.category_id = data.category_id

    for field in ("name", "description", "brand", "barcode", "weight", "height", "width", "depth"):
        value = getattr(data, field)
        if value is not None:
            setattr(product, field, value)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="SKU or barcode already used in this filial",
        ) from exc
    db.refresh(product)
    return product


def deactivate_product(db: Session, product: Product) -> Product:
    product.status = RecordStatus.INACTIVE
    db.commit()
    db.refresh(product)
    return product


# ---------------------------------------------------------------------
# product cost — 1:1, current value only (history lives in order_items.cost_at_sale)
# ---------------------------------------------------------------------


def get_product_cost(db: Session, product_id: int) -> ProductCost | None:
    return db.execute(
        select(ProductCost).where(ProductCost.product_id == product_id)
    ).scalar_one_or_none()


def get_product_costs_bulk(db: Session, product_ids: list[int]) -> dict[int, ProductCost]:
    if not product_ids:
        return {}
    rows = db.execute(select(ProductCost).where(ProductCost.product_id.in_(product_ids))).scalars().all()
    return {cost.product_id: cost for cost in rows}


def set_product_cost(db: Session, product_id: int, cost_value: Decimal) -> ProductCost:
    cost = get_product_cost(db, product_id)
    if cost is None:
        cost = ProductCost(product_id=product_id, cost_value=cost_value)
        db.add(cost)
    else:
        cost.cost_value = cost_value
    db.commit()
    db.refresh(cost)
    return cost


# ---------------------------------------------------------------------
# prices — append-only log, never UPDATE in place
# ---------------------------------------------------------------------


def get_current_price(db: Session, product_id: int) -> Price | None:
    """The price row valid *today* (business timezone)."""
    return get_price_at(db, product_id, local_today())


def get_price_at(db: Session, product_id: int, day: date) -> Price | None:
    """The price row valid on `day` — start_date <= day <= end_date (either bound may be
    open/NULL). Checking only `end_date IS NULL` (as an earlier version of this function
    did) would wrongly skip a row whose end_date is a real, still-future date. Used with a
    past day for offline sales: they're priced as of when they happened, not when synced."""
    return (
        db.execute(
            select(Price)
            .where(
                Price.product_id == product_id,
                or_(Price.start_date.is_(None), Price.start_date <= day),
                or_(Price.end_date.is_(None), Price.end_date >= day),
            )
            .order_by(Price.start_date.desc(), Price.created_at.desc())
        )
        .scalars()
        .first()
    )


def get_current_prices_bulk(db: Session, product_ids: list[int]) -> dict[int, Price]:
    """Same validity rule as get_current_price, batched into one query to avoid N+1 when
    building a page of products. If more than one row is still valid for a product (should
    not normally happen, set_price always closes the previous one), the most recently
    started one wins — same tiebreak as the single-product lookup."""
    if not product_ids:
        return {}
    today = local_today()
    rows = (
        db.execute(
            select(Price)
            .where(
                Price.product_id.in_(product_ids),
                or_(Price.start_date.is_(None), Price.start_date <= today),
                or_(Price.end_date.is_(None), Price.end_date >= today),
            )
            .order_by(Price.product_id, Price.start_date.desc(), Price.created_at.desc())
        )
        .scalars()
        .all()
    )
    result: dict[int, Price] = {}
    for price in rows:
        result.setdefault(price.product_id, price)
    return result


def set_price(
    db: Session,
    product: Product,
    price_default: Decimal,
    price_offer: Decimal,
    start_date: date | None,
    end_date: date | None,
) -> Price:
    effective_start = start_date or local_today()

    current = get_current_price(db, product.id)
    if current is not None:
        # Closing the current price before its own start would invert its validity window
        # (the DB rejects it: ck_prices_date_range) — reject it here as a clean 422.
        if current.start_date is not None and effective_start < current.start_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"start_date {effective_start} is before the current price's start "
                f"({current.start_date}) — a new price can only start on or after it",
            )
        current.end_date = effective_start

    new_price = Price(
        product_id=product.id,
        filial_id=product.filial_id,
        price_default=price_default,
        price_offer=price_offer,
        start_date=effective_start,
        end_date=end_date,
    )
    db.add(new_price)
    db.commit()
    db.refresh(new_price)
    return new_price


# ---------------------------------------------------------------------
# stock — never change quantity without a stock_movements row in the same transaction
# ---------------------------------------------------------------------


def lock_stocks(db: Session, filial_id: int, product_ids) -> dict[int, Stock]:
    """Locks (SELECT ... FOR UPDATE) the stock rows of these products until the end of the
    transaction, so a concurrent sale/cancel/adjustment waits instead of overwriting the
    quantity with a value computed from a stale read (lost update). Always locked in
    product_id order, so two transactions never deadlock on each other's rows."""
    ids = sorted(set(product_ids))
    if not ids:
        return {}
    rows = db.execute(
        select(Stock)
        .where(Stock.filial_id == filial_id, Stock.product_id.in_(ids))
        .order_by(Stock.product_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().all()
    return {stock.product_id: stock for stock in rows}


def lock_or_create_stock(db: Session, product_id: int, filial_id: int) -> Stock:
    """Like lock_stocks for one product, creating its (quantity 0) row first if missing —
    ON CONFLICT DO NOTHING, so two concurrent first adjustments can't both insert it."""
    db.execute(
        pg_insert(Stock)
        .values(product_id=product_id, filial_id=filial_id, quantity=0)
        .on_conflict_do_nothing(index_elements=["product_id", "filial_id"])
    )
    return lock_stocks(db, filial_id, [product_id])[product_id]


def get_stock(db: Session, product: Product) -> Stock | None:
    return db.execute(
        select(Stock).where(Stock.product_id == product.id, Stock.filial_id == product.filial_id)
    ).scalar_one_or_none()


def get_stocks_bulk(db: Session, product_ids: list[int]) -> dict[int, Stock]:
    """Filtering by product_id alone is enough — adjust_stock always writes Stock.filial_id
    equal to the product's own filial_id, so there is at most one row per product_id."""
    if not product_ids:
        return {}
    rows = db.execute(select(Stock).where(Stock.product_id.in_(product_ids))).scalars().all()
    return {stock.product_id: stock for stock in rows}


def adjust_stock(
    db: Session, product: Product, quantity_delta: int, reason: StockMovementReason
) -> Stock:
    if quantity_delta == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="quantity_delta must not be zero"
        )

    stock = lock_or_create_stock(db, product.id, product.filial_id)
    new_qty = stock.quantity + quantity_delta
    if new_qty < 0:
        db.rollback()  # releases the lock (and the placeholder row, if it was just created)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Adjustment would make stock negative"
        )
    stock.quantity = new_qty

    db.add(
        StockMovement(
            product_id=product.id,
            filial_id=product.filial_id,
            change_qty=quantity_delta,
            reason=reason,
            resulting_qty=new_qty,
        )
    )
    db.commit()
    db.refresh(stock)
    return stock
