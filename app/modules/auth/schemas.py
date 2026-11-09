from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserRead(BaseModel):
    id: int
    username: str
    full_name: str
    email: str
    is_superuser: bool
    accessible_filial_ids: list[int]
    # Deliberate trade-off, not an oversight: exposes the CALLER'S OWN bcrypt hash (never
    # another user's — /auth/me only ever returns the authenticated user). Requested
    # explicitly despite the OWASP API3 (Excessive Data Exposure) risk this carries: if
    # this response is ever logged, cached on a compromised device, or otherwise leaked,
    # the hash becomes offline-crackable material, especially against a weak password
    # (e.g. the seeded admin123). No endpoint anywhere else in this codebase returns it.
    password_hash: str


# bcrypt only uses the first 72 bytes of a password — reject longer ones instead of silently
# ignoring the tail.
Password = Annotated[str, Field(min_length=6, max_length=72)]


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=72)
    new_password: Password
