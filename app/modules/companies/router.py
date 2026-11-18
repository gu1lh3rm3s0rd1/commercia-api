from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.deps import get_accessible_filial_ids, get_current_user
from app.db.session import get_db
from app.models.user import User
from app.modules.companies import service
from app.modules.companies.schemas import CompanyRead, PlanUpdate

router = APIRouter()


@router.get("", response_model=list[CompanyRead])
def list_companies(
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> list[CompanyRead]:
    return service.list_companies(db, accessible)


@router.patch("/{filial_id}/plan", response_model=CompanyRead)
def change_plan(
    filial_id: int,
    data: PlanUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CompanyRead:
    """Superuser only (no real payment in the TCC). Logged as company.plan_change."""
    if not user.is_superuser:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only a superuser changes a store's plan")
    return service.change_plan(db, filial_id, data.plan)
