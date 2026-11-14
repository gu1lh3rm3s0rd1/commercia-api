"""People registry (Etapa 2 of clients → people). One person per registration of a store,
with one or more roles (app/core/people.py) and pessoa física (CPF) or jurídica (CNPJ)."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.people import PERSON_ROLES
from app.models.enums import PersonType, RecordStatus
from app.modules.clients.schemas import CPF_PATTERN, ORMModel

CNPJ_PATTERN = r"^[0-9]{14}$"


def _check_roles(roles: list[str] | None) -> list[str] | None:
    if roles is None:
        return None
    unknown = sorted({r for r in roles if r not in PERSON_ROLES})
    if unknown:
        raise ValueError(f"unknown role(s) {unknown} — valid: {sorted(PERSON_ROLES)}")
    return sorted(set(roles))  # no duplicates, stable order


class PersonCreate(BaseModel):
    """individual → cpf, first_name, last_name (birth_date optional);
    company → cnpj, corporate_name (trade_name optional)."""

    model_config = ConfigDict(extra="forbid")

    person_type: PersonType = PersonType.INDIVIDUAL
    roles: list[str] = Field(min_length=1, description=f"one or more of: {', '.join(PERSON_ROLES)}")
    first_name: str | None = Field(None, min_length=1, max_length=120)
    last_name: str | None = Field(None, min_length=1, max_length=120)
    corporate_name: str | None = Field(None, min_length=1, max_length=160)
    trade_name: str | None = Field(None, min_length=1, max_length=160)
    cpf: str | None = Field(None, pattern=CPF_PATTERN)
    cnpj: str | None = Field(None, pattern=CNPJ_PATTERN)
    email: str | None = Field(None, max_length=160)
    phone: str | None = Field(None, max_length=20)
    birth_date: date | None = None

    @field_validator("roles")
    @classmethod
    def _valid_roles(cls, roles):
        return _check_roles(roles)

    @model_validator(mode="after")
    def _document_matches_type(self) -> "PersonCreate":
        if self.person_type == PersonType.INDIVIDUAL:
            missing = [f for f in ("cpf", "first_name", "last_name") if getattr(self, f) is None]
            extra = [f for f in ("cnpj", "corporate_name", "trade_name") if getattr(self, f) is not None]
        else:
            missing = [f for f in ("cnpj", "corporate_name") if getattr(self, f) is None]
            extra = [f for f in ("cpf", "first_name", "last_name", "birth_date") if getattr(self, f) is not None]
        if missing:
            raise ValueError(f"{self.person_type.value} requires: {', '.join(missing)}")
        if extra:
            raise ValueError(f"{self.person_type.value} doesn't take: {', '.join(extra)}")
        return self


class PersonUpdate(BaseModel):
    """Only the fields sent change. `roles` replaces the whole list. person_type can't change
    (a CPF person doesn't become a CNPJ one — register the company instead).
    status='active' reactivates; 'inactive' is the same as DELETE."""

    model_config = ConfigDict(extra="forbid")

    roles: list[str] | None = Field(None, min_length=1)
    first_name: str | None = Field(None, min_length=1, max_length=120)
    last_name: str | None = Field(None, min_length=1, max_length=120)
    corporate_name: str | None = Field(None, min_length=1, max_length=160)
    trade_name: str | None = Field(None, min_length=1, max_length=160)
    cpf: str | None = Field(None, pattern=CPF_PATTERN)
    cnpj: str | None = Field(None, pattern=CNPJ_PATTERN)
    email: str | None = Field(None, max_length=160)
    phone: str | None = Field(None, max_length=20)
    birth_date: date | None = None
    status: RecordStatus | None = None

    @field_validator("roles")
    @classmethod
    def _valid_roles(cls, roles):
        return _check_roles(roles)


class PersonRead(ORMModel):
    id: int
    person_type: PersonType
    roles: list[str]
    display_name: str  # trade/corporate name for a company, "first last" for an individual
    first_name: str | None
    last_name: str | None
    corporate_name: str | None
    trade_name: str | None
    cpf: str | None
    cnpj: str | None
    email: str | None
    phone: str | None
    birth_date: date | None
    status: RecordStatus
    filial_id: int
    created_at: datetime
    updated_at: datetime


class PersonAddressRead(ORMModel):
    id: int
    person_id: int
    street: str
    number: str | None
    complement: str | None
    neighborhood: str | None
    city: str
    state: str
    zip_code: str
