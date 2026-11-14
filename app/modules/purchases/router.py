"""/purchases (Etapa 3) — every route needs purchases.manage (Administrador, Gerente):
purchases carry costs and supplier terms, which sellers don't see."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.deps import (
    get_accessible_filial_ids,
    get_scoped_filial_optional,
    get_scoped_filial_required,
    require_permission,
)
from app.db.session import get_db
from app.models.enums import PurchaseStatus
from app.models.purchases import Purchase, PurchaseItem
from app.models.user import User
from app.modules.catalog.schemas import Page
from app.modules.purchases import service
from app.modules.purchases.schemas import PurchaseCreate, PurchaseItemRead, PurchaseRead

router = APIRouter()

_manager = require_permission("purchases.manage")


def _read(purchase: Purchase, items: list[PurchaseItem]) -> PurchaseRead:
    return PurchaseRead.model_validate(purchase).model_copy(
        update={"items": [PurchaseItemRead.model_validate(i) for i in items]}
    )


@router.post("", response_model=PurchaseRead, status_code=status.HTTP_201_CREATED)
def create_purchase(
    data: PurchaseCreate,
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    user: User = Depends(_manager),
) -> PurchaseRead:
    purchase, items = service.create_purchase(db, filial_id, user.id, data)
    return _read(purchase, items)


@router.get("", response_model=Page[PurchaseRead])
def list_purchases(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    supplier_id: int | None = None,
    purchase_status: PurchaseStatus | None = Query(None, alias="status"),
    start_date: date | None = Query(None, description="business-timezone day the purchase happened, inclusive"),
    end_date: date | None = Query(None, description="business-timezone day the purchase happened, inclusive"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(_manager),
) -> Page[PurchaseRead]:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail="start_date must be <= end_date")
    filial_ids = {filial_id} if filial_id is not None else accessible
    rows, total = service.list_purchases(db, filial_ids, supplier_id, purchase_status, start_date, end_date, limit, offset)
    items = service.get_items_bulk(db, [p.id for p in rows])
    return Page(items=[_read(p, items[p.id]) for p in rows], total=total, limit=limit, offset=offset)


@router.get("/{purchase_id}", response_model=PurchaseRead)
def get_purchase(
    purchase_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(_manager),
) -> PurchaseRead:
    purchase = service.get_purchase_or_404(db, purchase_id, accessible)
    return _read(purchase, service.get_items(db, purchase.id))


@router.post("/{purchase_id}/receive", response_model=PurchaseRead)
def receive_purchase(
    purchase_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    user: User = Depends(_manager),
) -> PurchaseRead:
    """Stock in + the products' cost becomes this purchase's landed cost (if it's the latest)."""
    purchase = service.receive_purchase(db, service.get_purchase_or_404(db, purchase_id, accessible), user.id)
    return _read(purchase, service.get_items(db, purchase.id))


@router.post("/{purchase_id}/cancel", response_model=PurchaseRead)
def cancel_purchase(
    purchase_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(_manager),
) -> PurchaseRead:
    """A received purchase's stock goes back out (409 if it was already sold); the cost stays."""
    purchase = service.cancel_purchase(db, service.get_purchase_or_404(db, purchase_id, accessible))
    return _read(purchase, service.get_items(db, purchase.id))
