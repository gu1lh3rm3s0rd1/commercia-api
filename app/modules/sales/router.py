from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.deps import (
    get_accessible_filial_ids,
    get_current_user,
    get_scoped_filial_optional,
    get_scoped_filial_required,
    require_permission,
)
from app.db.session import get_db
from app.models.enums import OrderStatus
from app.models.sales import Order, OrderItem
from app.models.user import User
from app.modules.catalog.schemas import Page
from app.modules.sales import service
from app.modules.sales.schemas import OrderCreate, OrderItemRead, OrderRead

router = APIRouter()

_MANAGE_ORDERS = [Depends(require_permission("orders.manage"))]


def _to_order_read(order: Order, items: list[OrderItem]) -> OrderRead:
    return OrderRead(
        id=order.id,
        client_id=order.client_id,
        filial_id=order.filial_id,
        total=order.total,
        discount=order.discount,
        payment_method=order.payment_method,
        status=order.status,
        client_reference=order.client_reference,
        occurred_at=order.occurred_at,
        created_at=order.created_at,
        updated_at=order.updated_at,
        items=[OrderItemRead.model_validate(item) for item in items],
    )


@router.post("", response_model=OrderRead, status_code=status.HTTP_201_CREATED, dependencies=_MANAGE_ORDERS)
def create_order(
    data: OrderCreate,
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OrderRead:
    order, items = service.create_order(db, filial_id, user.id, data)
    return _to_order_read(order, items)


@router.get("", response_model=Page[OrderRead])
def list_orders(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    order_status: OrderStatus | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> Page[OrderRead]:
    filial_ids = {filial_id} if filial_id is not None else accessible
    orders, total = service.list_orders(db, filial_ids, order_status, limit, offset)
    items_page = [_to_order_read(o, service.get_order_items(db, o.id)) for o in orders]
    return Page(items=items_page, total=total, limit=limit, offset=offset)


@router.get("/{order_id}", response_model=OrderRead)
def get_order(
    order_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> OrderRead:
    order, items = service.get_order_with_items(db, order_id, accessible)
    return _to_order_read(order, items)


@router.post("/{order_id}/pay", response_model=OrderRead, dependencies=_MANAGE_ORDERS)
def pay_order(
    order_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> OrderRead:
    order = service.get_order_or_404(db, order_id, accessible)
    order = service.mark_as_paid(db, order)
    return _to_order_read(order, service.get_order_items(db, order_id))


@router.post("/{order_id}/cancel", response_model=OrderRead, dependencies=_MANAGE_ORDERS)
def cancel_order(
    order_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> OrderRead:
    order = service.get_order_or_404(db, order_id, accessible)
    order = service.cancel_order(db, order)
    return _to_order_read(order, service.get_order_items(db, order_id))
