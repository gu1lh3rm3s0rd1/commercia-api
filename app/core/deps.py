from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import InvalidTokenError, decode_access_token
from app.db.session import get_db
from app.models.company import Company
from app.models.user import GroupPermission, Permission, User, UserCompany, UserGroup

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    try:
        user_id = decode_access_token(token)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = db.get(User, int(user_id))
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive"
        )
    return user


def get_accessible_filial_ids(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> set[int]:
    """Every filial_id this user is allowed to touch. A superuser accesses every filial
    without needing an explicit user_companies row per store."""
    if user.is_superuser:
        return set(db.execute(select(Company.id)).scalars().all())
    return set(
        db.execute(select(UserCompany.filial_id).where(UserCompany.user_id == user.id))
        .scalars()
        .all()
    )


def get_scoped_filial_optional(
    filial_id: int | None = None,
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> int | None:
    """For READ endpoints: filial_id is an optional query param. Omit it to mean
    'aggregate across every filial this user can access'."""
    if filial_id is not None and filial_id not in accessible:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this filial")
    return filial_id


def get_scoped_filial_required(
    filial_id: int,
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> int:
    """For WRITE endpoints: filial_id is a mandatory query param and must be one of the
    filiais this user can access."""
    if filial_id not in accessible:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this filial")
    return filial_id


def require_permission(code: str):
    """Dependency factory: Depends(require_permission("products.edit")).
    A superuser always passes; everyone else needs the permission via one of their groups."""

    def _checker(
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        if user.is_superuser:
            return user

        stmt = (
            select(Permission.code)
            .join(GroupPermission, GroupPermission.permission_id == Permission.id)
            .join(UserGroup, UserGroup.group_id == GroupPermission.group_id)
            .where(UserGroup.user_id == user.id, Permission.code == code)
        )
        if db.execute(stmt).first() is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing permission")
        return user

    return _checker
