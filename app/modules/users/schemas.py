from datetime import datetime

from pydantic import BaseModel


class UserRead(BaseModel):
    id: int
    username: str
    email: str
    password_hash: str
    is_active: bool
    is_superuser: bool
    created_at: datetime
    last_login: datetime | None
    accessible_filial_ids: list[int]
