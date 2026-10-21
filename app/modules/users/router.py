from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import require_permission
from app.db.session import get_db
from app.modules.catalog.schemas import Page
from app.modules.users import service
from app.modules.users.schemas import UserRead

router = APIRouter()

_MANAGE_USERS = [Depends(require_permission("users.manage"))]


@router.get("", response_model=Page[UserRead], dependencies=_MANAGE_USERS)
def list_users(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> Page[UserRead]:
    items, total = service.list_users(db, limit, offset)
    accessible_map = service.get_accessible_filial_ids_bulk(db, items)
    page_items = [
        UserRead(
            id=user.id,
            username=user.username,
            email=user.email,
            password_hash=user.password_hash,
            is_active=user.is_active,
            is_superuser=user.is_superuser,
            created_at=user.created_at,
            last_login=user.last_login,
            accessible_filial_ids=accessible_map.get(user.id, []),
        )
        for user in items
    ]
    return Page(items=page_items, total=total, limit=limit, offset=offset)
