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
