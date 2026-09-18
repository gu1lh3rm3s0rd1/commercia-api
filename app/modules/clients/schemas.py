from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import RecordStatus

CPF_PATTERN = r"^[0-9]{11}$"
ZIP_PATTERN = r"^[0-9]{8}$"


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- clients ---
# cpf/zip_code get the same format validation here as the DB's own CHECK constraints
# (ck_clients_cpf_format / ck_client_addresses_zip_format) — catching it in Pydantic
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
    id: int
    first_name: str
    last_name: str
    email: str
    cpf: str
    birth_date: date | None
    phone: str | None
    status: RecordStatus
    filial_id: int
    created_at: datetime


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
    client_id: int
    street: str
    number: str | None
    complement: str | None
    neighborhood: str | None
    city: str
    state: str
    zip_code: str
