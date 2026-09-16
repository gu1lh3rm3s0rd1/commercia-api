from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from app.core.config import get_settings

_ALGORITHM = "HS256"


class InvalidTokenError(Exception):
    """Raised when a JWT is malformed, expired, or missing its subject."""


def hash_password(password: str) -> str:
    # Using bcrypt directly rather than passlib: passlib is unmaintained and its internal
    # self-test is incompatible with bcrypt>=4.1 (raises ValueError on import-time check).
    # bcrypt's own 72-byte secret limit still applies — fine for real passwords typed by a
    # user; a create-user endpoint should validate max length before calling this.
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))


def create_access_token(subject: str) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": subject, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> str:
    """Returns the subject (user id, as string) encoded in the token."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[_ALGORITHM])
    except JWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    subject = payload.get("sub")
    if subject is None:
        raise InvalidTokenError("token missing subject")
    return subject
