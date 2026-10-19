from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.deps import get_accessible_filial_ids, get_current_user
from app.db.session import get_db
from app.models.user import User
from app.modules.companies import service
from app.modules.companies.schemas import CompanyRead

router = APIRouter()


@router.get("", response_model=list[CompanyRead])
def list_companies(
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> list[CompanyRead]:
    return service.list_companies(db, accessible)
