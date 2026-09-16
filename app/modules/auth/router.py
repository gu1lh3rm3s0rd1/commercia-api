from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.core.deps import get_accessible_filial_ids, get_current_user
from app.core.security import create_access_token
from app.db.session import get_db
from app.models.user import User
from app.modules.auth.schemas import TokenResponse, UserRead
from app.modules.auth.service import authenticate_user

router = APIRouter()


@router.post("/login", response_model=TokenResponse)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> TokenResponse:
    user = authenticate_user(db, form_data.username, form_data.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenResponse(access_token=create_access_token(subject=str(user.id)))


@router.get("/me", response_model=UserRead)
def read_current_user(
    user: User = Depends(get_current_user),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> UserRead:
    return UserRead(
        id=user.id,
        username=user.username,
        email=user.email,
        is_superuser=user.is_superuser,
        accessible_filial_ids=sorted(accessible),
    )
