"""User management (RF01 / UC002) — every route requires users.manage. Fixed access
profiles (Administrador / Gerente / Vendedor, see app/core/roles.py) are listed by
/users/groups; a user's own password change lives at POST /auth/me/password."""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.deps import get_accessible_filial_ids, require_permission
from app.db.session import get_db
from app.models.user import User
from app.modules.catalog.schemas import Page
from app.modules.users import service
from app.modules.users.schemas import (
    GroupRead,
    PasswordReset,
    PermissionRead,
    UserCreate,
    UserRead,
    UserUpdate,
)

router = APIRouter()

_manager = require_permission("users.manage")


@router.get("", response_model=Page[UserRead])
def list_users(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    caller: User = Depends(_manager),
) -> Page[UserRead]:
    items, total = service.list_users(db, accessible, caller.is_superuser, limit, offset)
    return Page(items=service.to_user_reads(db, items), total=total, limit=limit, offset=offset)


# declared before /{user_id} so "groups"/"permissions" aren't read as a user id
@router.get("/groups", response_model=list[GroupRead])
def list_groups(db: Session = Depends(get_db), _caller: User = Depends(_manager)) -> list[GroupRead]:
    return [GroupRead(id=g.id, name=g.name, permissions=codes) for g, codes in service.list_groups(db)]


@router.get("/permissions", response_model=list[PermissionRead])
def list_permissions(db: Session = Depends(get_db), _caller: User = Depends(_manager)) -> list[PermissionRead]:
    return [PermissionRead.model_validate(p) for p in service.list_permissions(db)]


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(
    data: UserCreate,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    caller: User = Depends(_manager),
) -> UserRead:
    user = service.create_user(db, caller, accessible, data)
    return service.to_user_reads(db, [user])[0]


@router.get("/{user_id}", response_model=UserRead)
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    caller: User = Depends(_manager),
) -> UserRead:
    user = service.get_user_or_404(db, user_id, accessible, caller)
    return service.to_user_reads(db, [user])[0]


@router.patch("/{user_id}", response_model=UserRead)
def update_user(
    user_id: int,
    data: UserUpdate,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    caller: User = Depends(_manager),
) -> UserRead:
    user = service.get_user_or_404(db, user_id, accessible, caller)
    user = service.update_user(db, caller, accessible, user, data)
    return service.to_user_reads(db, [user])[0]


@router.delete("/{user_id}", response_model=UserRead)
def deactivate_user(
    user_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    caller: User = Depends(_manager),
) -> UserRead:
    """Deactivates (is_active=false) — never deletes: orders and audit logs point at the
    user. Takes effect immediately (tokens of an inactive user are refused) and, on the
    app, at the next pull (users carry is_active)."""
    user = service.get_user_or_404(db, user_id, accessible, caller)
    user = service.deactivate_user(db, caller, user)
    return service.to_user_reads(db, [user])[0]


@router.post("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(
    user_id: int,
    data: PasswordReset,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    caller: User = Depends(_manager),
) -> None:
    user = service.get_user_or_404(db, user_id, accessible, caller)
    service.reset_password(db, caller, user, data.new_password)
