from datetime import datetime, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.security import hash_password, verify_password
from app.models.user import User


def authenticate_user(db: Session, username: str, password: str) -> User | None:
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def login(db: Session, request: Request, username: str, password: str) -> User | None:
    """Authenticates and records the attempt (RNF11 access log). A success also stamps
    users.last_login. Failures are logged too — with the username that was tried — and
    committed on their own, since there's no operation for them to ride along with."""
    audit.bind_request(db, None, request)
    user = authenticate_user(db, username, password)
    if user is None:
        known = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
        audit.record(db, "auth.login_failed", entity_type="user",
                     entity_id=known.id if known is not None else None,
                     details={"username": username[:60],
                              "reason": "inactive" if known is not None and not known.is_active else "invalid"},
                     user=known)
        db.commit()
        return None
    user.last_login = datetime.now(timezone.utc)
    audit.record(db, "auth.login", entity_type="user", entity_id=user.id, user=user)
    db.commit()
    db.refresh(user)
    return user


def change_own_password(db: Session, user: User, current_password: str, new_password: str) -> None:
    if not verify_password(current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    user.password_hash = hash_password(new_password)
    audit.record(db, "user.password_change", entity_type="user", entity_id=user.id)
    db.commit()

