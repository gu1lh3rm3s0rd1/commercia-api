from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.modules.auth.schemas import Password

# Usernames are the offline-login key on the mobile app: plain, no spaces.
Username = Annotated[str, Field(min_length=3, max_length=60, pattern=r"^[A-Za-z0-9._-]+$")]
FullName = Annotated[str, Field(min_length=1, max_length=120)]
Email = Annotated[str, Field(max_length=160, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]


class GroupBrief(BaseModel):
    id: int
    name: str


class UserRead(BaseModel):
    id: int
    username: str
    full_name: str
    email: str
    password_hash: str
    is_active: bool
    is_superuser: bool
    created_at: datetime
    last_login: datetime | None
    accessible_filial_ids: list[int]
    groups: list[GroupBrief]


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: FullName
    username: Username
    email: Email
    password: Password
    group_ids: list[int] = Field(min_length=1)
    filial_ids: list[int] = Field(min_length=1)


class UserUpdate(BaseModel):
    """Every field optional; username can't change (it's the offline-login key and
    appears in the audit trail). is_active=true reactivates a deactivated user."""

    model_config = ConfigDict(extra="forbid")

    full_name: FullName | None = None
    email: Email | None = None
    group_ids: list[int] | None = Field(None, min_length=1)
    filial_ids: list[int] | None = Field(None, min_length=1)
    is_active: bool | None = None


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_password: Password


class PermissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str


class GroupRead(BaseModel):
    id: int
    name: str
    permissions: list[str]
