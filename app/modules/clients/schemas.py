from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import PersonType, RecordStatus

CPF_PATTERN = r"^[0-9]{11}$"
ZIP_PATTERN = r"^[0-9]{8}$"


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- clients ---
# /clients is the compatibility view of `people` (migration 0013): people with the "client"
# role, same routes and fields as before. Creating here always makes a pessoa física with
# roles=[client]; the general registry (PJ, suppliers, several roles) is /people.
# cpf/zip_code get the same format validation here as the DB's own CHECK constraints
# (ck_people_cpf_format / ck_person_addresses_zip_format) — catching it in Pydantic
# gives a clean 422 instead of an IntegrityError that the service would otherwise have
# to (mis)classify as a duplicate.


class ClientCreate(BaseModel):
    first_name: str
    last_name: str
    email: str
    cpf: str = Field(pattern=CPF_PATTERN)
    birth_date: date | None = None
    phone: str | None = None


class ClientUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    cpf: str | None = Field(default=None, pattern=CPF_PATTERN)
    birth_date: date | None = None
    phone: str | None = None


class ClientRead(ORMModel):
    """first_name/last_name/cpf are null only for a pessoa jurídica client, and e-mail may be
    null — both only for clients registered through /people. Everything a /clients POST
    creates still has all four."""

    id: int
    first_name: str | None
    last_name: str | None
    email: str | None
    cpf: str | None
    birth_date: date | None
    phone: str | None
    status: RecordStatus
    filial_id: int
    created_at: datetime
    # added with migration 0013 (additive)
    person_type: PersonType
    roles: list[str]
    cnpj: str | None
    corporate_name: str | None
    trade_name: str | None
    updated_at: datetime


# --- client addresses ---


class ClientAddressCreate(BaseModel):
    street: str
    number: str | None = None
    complement: str | None = None
    neighborhood: str | None = None
    city: str
    state: str
    zip_code: str = Field(pattern=ZIP_PATTERN)


class ClientAddressUpdate(BaseModel):
    street: str | None = None
    number: str | None = None
    complement: str | None = None
    neighborhood: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = Field(default=None, pattern=ZIP_PATTERN)


class ClientAddressRead(ORMModel):
    id: int
    client_id: int = Field(validation_alias="person_id")  # column renamed in 0013; same API field
    street: str
    number: str | None
    complement: str | None
    neighborhood: str | None
    city: str
    state: str
    zip_code: str
