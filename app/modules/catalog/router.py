from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.deps import (
    get_accessible_filial_ids,
    get_current_user,
    get_scoped_filial_optional,
    get_scoped_filial_required,
    require_permission,
)
from app.db.session import get_db
from app.models.enums import StockMovementReason
from app.models.user import User
from app.modules.catalog import service
from app.modules.catalog.schemas import (
    CategoryCreate,
    CategoryRead,
    CategoryUpdate,
    DepartmentCreate,
    DepartmentRead,
    Page,
    PriceRead,
    PriceSet,
    ProductCostRead,
    ProductCostSet,
    ProductCreate,
    ProductRead,
    ProductUpdate,
    StockAdjust,
    StockRead,
)

router = APIRouter()

_EDIT_PRODUCTS = [Depends(require_permission("products.edit"))]


# ---------------------------------------------------------------------
# departments
# ---------------------------------------------------------------------


@router.get("/departments", response_model=list[DepartmentRead])
def list_departments(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[DepartmentRead]:
    return service.list_departments(db)


@router.post(
    "/departments",
    response_model=DepartmentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=_EDIT_PRODUCTS,
)
def create_department(
    data: DepartmentCreate, db: Session = Depends(get_db)
) -> DepartmentRead:
    return service.create_department(db, data.name)


# ---------------------------------------------------------------------
# categories
# ---------------------------------------------------------------------


@router.get("/categories", response_model=Page[CategoryRead])
def list_categories(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> Page[CategoryRead]:
    filial_ids = {filial_id} if filial_id is not None else accessible
    items, total = service.list_categories(db, filial_ids, limit, offset)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.post(
    "/categories",
    response_model=CategoryRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=_EDIT_PRODUCTS,
)
def create_category(
    data: CategoryCreate,
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CategoryRead:
    return service.create_category(
        db, filial_id, data.name, data.department_id, created_by_id=user.id
    )


@router.get("/categories/{category_id}", response_model=CategoryRead)
def get_category(
    category_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> CategoryRead:
    return service.get_category_or_404(db, category_id, accessible)


@router.patch(
    "/categories/{category_id}",
    response_model=CategoryRead,
    dependencies=_EDIT_PRODUCTS,
)
def update_category(
    category_id: int,
    data: CategoryUpdate,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> CategoryRead:
    category = service.get_category_or_404(db, category_id, accessible)
    return service.update_category(db, category, data.name, data.department_id)


# ---------------------------------------------------------------------
# products
# ---------------------------------------------------------------------


@router.get("/products", response_model=Page[ProductRead])
def list_products(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> Page[ProductRead]:
    filial_ids = {filial_id} if filial_id is not None else accessible
    items, total = service.list_products(db, filial_ids, limit, offset)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.post(
    "/products",
    response_model=ProductRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=_EDIT_PRODUCTS,
)
def create_product(
    data: ProductCreate,
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProductRead:
    return service.create_product(db, filial_id, user.id, data)


@router.get("/products/{product_id}", response_model=ProductRead)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> ProductRead:
    product = service.get_product_or_404(db, product_id)
    service.ensure_filial_access(product.filial_id, accessible)
    return product


@router.patch(
    "/products/{product_id}", response_model=ProductRead, dependencies=_EDIT_PRODUCTS
)
def update_product(
    product_id: int,
    data: ProductUpdate,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> ProductRead:
    product = service.get_product_or_404(db, product_id)
    service.ensure_filial_access(product.filial_id, accessible)
    return service.update_product(db, product, data)


@router.delete(
    "/products/{product_id}", response_model=ProductRead, dependencies=_EDIT_PRODUCTS
)
def deactivate_product(
    product_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> ProductRead:
    """'Delete' means status = inactive (soft delete) — products are referenced by
    historical sales data and are never hard-deleted."""
    product = service.get_product_or_404(db, product_id)
    service.ensure_filial_access(product.filial_id, accessible)
    return service.deactivate_product(db, product)


# ---------------------------------------------------------------------
# product cost
# ---------------------------------------------------------------------


@router.get(
    "/products/{product_id}/cost",
    response_model=ProductCostRead,
    dependencies=_EDIT_PRODUCTS,
)
def get_product_cost(
    product_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> ProductCostRead:
    product = service.get_product_or_404(db, product_id)
    service.ensure_filial_access(product.filial_id, accessible)
    cost = service.get_product_cost(db, product_id)
    if cost is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cost not set for this product",
        )
    return cost


@router.put(
    "/products/{product_id}/cost",
    response_model=ProductCostRead,
    dependencies=_EDIT_PRODUCTS,
)
def set_product_cost(
    product_id: int,
    data: ProductCostSet,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> ProductCostRead:
    product = service.get_product_or_404(db, product_id)
    service.ensure_filial_access(product.filial_id, accessible)
    return service.set_product_cost(db, product_id, data.cost_value)


# ---------------------------------------------------------------------
# prices
# ---------------------------------------------------------------------


@router.get("/products/{product_id}/prices/current", response_model=PriceRead)
def get_current_price(
    product_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> PriceRead:
    product = service.get_product_or_404(db, product_id)
    service.ensure_filial_access(product.filial_id, accessible)
    price = service.get_current_price(db, product_id)
    if price is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No price set for this product",
        )
    return price


@router.post(
    "/products/{product_id}/prices",
    response_model=PriceRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=_EDIT_PRODUCTS,
)
def set_price(
    product_id: int,
    data: PriceSet,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> PriceRead:
    product = service.get_product_or_404(db, product_id)
    service.ensure_filial_access(product.filial_id, accessible)
    return service.set_price(
        db,
        product,
        data.price_default,
        data.price_offer,
        data.start_date,
        data.end_date,
    )


# ---------------------------------------------------------------------
# stock
# ---------------------------------------------------------------------


@router.get("/products/{product_id}/stock", response_model=StockRead)
def get_stock(
    product_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> StockRead:
    product = service.get_product_or_404(db, product_id)
    service.ensure_filial_access(product.filial_id, accessible)
    stock = service.get_stock(db, product)
    if stock is None:
        return StockRead(product_id=product_id, filial_id=product.filial_id, quantity=0)
    return stock


@router.post(
    "/products/{product_id}/stock/adjustments",
    response_model=StockRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=_EDIT_PRODUCTS,
)
def adjust_stock(
    product_id: int,
    data: StockAdjust,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> StockRead:
    product = service.get_product_or_404(db, product_id)
    service.ensure_filial_access(product.filial_id, accessible)
    reason = StockMovementReason(data.reason.value)
    return service.adjust_stock(db, product, data.quantity_delta, reason)
