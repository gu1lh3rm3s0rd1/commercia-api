from pydantic import BaseModel


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserRead(BaseModel):
    id: int
    username: str
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
