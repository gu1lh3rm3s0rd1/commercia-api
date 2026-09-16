from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.catalog import Category, Department, Price, Product, ProductCost, Stock
from app.models.enums import RecordStatus, StockMovementReason
from app.models.sales import StockMovement
from app.modules.catalog.schemas import ProductCreate, ProductUpdate

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
    items = db.execute(base.order_by(Category.name).limit(limit).offset(offset)).scalars().all()
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
    items = db.execute(base.order_by(Product.name).limit(limit).offset(offset)).scalars().all()
    return list(items), total


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
    return (
        db.execute(
            select(Price)
            .where(Price.product_id == product_id, Price.end_date.is_(None))
            .order_by(Price.start_date.desc(), Price.created_at.desc())
        )
        .scalars()
        .first()
    )


def set_price(
    db: Session,
    product: Product,
    price_default: Decimal,
    price_offer: Decimal | None,
    start_date: date | None,
    end_date: date | None,
) -> Price:
    effective_start = start_date or date.today()

    current = get_current_price(db, product.id)
    if current is not None:
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


def get_stock(db: Session, product: Product) -> Stock | None:
    return db.execute(
        select(Stock).where(Stock.product_id == product.id, Stock.filial_id == product.filial_id)
    ).scalar_one_or_none()


def adjust_stock(
    db: Session, product: Product, quantity_delta: int, reason: StockMovementReason
) -> Stock:
    if quantity_delta == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="quantity_delta must not be zero"
        )

    stock = get_stock(db, product)
    current_qty = stock.quantity if stock is not None else 0
    new_qty = current_qty + quantity_delta
    if new_qty < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Adjustment would make stock negative"
        )

    if stock is None:
        stock = Stock(product_id=product.id, filial_id=product.filial_id, quantity=new_qty)
        db.add(stock)
    else:
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
