from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.core.deps import get_accessible_filial_ids, get_current_user
from app.core.security import create_access_token
from app.db.session import get_db
from app.models.user import User
from app.modules.auth import service
from app.modules.auth.schemas import PasswordChange, TokenResponse, UserRead

router = APIRouter()


@router.post("/login", response_model=TokenResponse)
def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> TokenResponse:
    user = service.login(db, request, form_data.username, form_data.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token, expire_at = create_access_token(subject=str(user.id))
    expires_in = int((expire_at - datetime.now(timezone.utc)).total_seconds())
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/me", response_model=UserRead)
def read_current_user(
    user: User = Depends(get_current_user),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> UserRead:
    return UserRead(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        email=user.email,
        is_superuser=user.is_superuser,
        accessible_filial_ids=sorted(accessible),
        password_hash=user.password_hash,
    )


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
def change_my_password(
    data: PasswordChange,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Any logged-in user changes their own password (the current one is required)."""
    service.change_own_password(db, user, data.current_password, data.new_password)
