"""Clients = people with the "client" role (migration 0013). Same behavior as before for
the routes, except the uniqueness rule: CPF is unique PER STORE (UC004), e-mail is not
unique anymore (decided 2026-11-14)."""

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.people import CLIENT
from app.models.enums import PersonType, RecordStatus
from app.models.sales import Person, PersonAddress
from app.modules.clients.schemas import ClientAddressCreate, ClientAddressUpdate, ClientCreate, ClientUpdate

_IS_CLIENT = Person.roles.contains([CLIENT])
_DUPLICATE_CPF = "CPF already registered in this filial"


def _cpf_taken(db: Session, filial_id: int, cpf: str, exclude_id: int | None = None) -> bool:
    stmt = select(Person.id).where(Person.filial_id == filial_id, Person.cpf == cpf)
    if exclude_id is not None:
        stmt = stmt.where(Person.id != exclude_id)
    return db.execute(stmt).first() is not None


def list_clients(db: Session, filial_ids: set[int], limit: int, offset: int) -> tuple[list[Person], int]:
    base = select(Person).where(Person.filial_id.in_(filial_ids), _IS_CLIENT)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    items = db.execute(
        base.order_by(Person.first_name, Person.last_name, Person.corporate_name, Person.id).limit(limit).offset(offset)
    ).scalars().all()
    return list(items), total


def create_client(db: Session, filial_id: int, created_by_id: int, data: ClientCreate) -> Person:
    if _cpf_taken(db, filial_id, data.cpf):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_DUPLICATE_CPF)

    client = Person(
        person_type=PersonType.INDIVIDUAL,
        roles=[CLIENT],
        first_name=data.first_name,
        last_name=data.last_name,
        email=data.email,
        cpf=data.cpf,
        birth_date=data.birth_date,
        phone=data.phone,
        filial_id=filial_id,
        created_by_id=created_by_id,
    )
    db.add(client)
    try:
        db.commit()
    except IntegrityError as exc:  # race between the pre-check and the insert
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_DUPLICATE_CPF) from exc
    db.refresh(client)
    return client


def get_client_or_404(db: Session, client_id: int, filial_ids: set[int]) -> Person:
    client = db.get(Person, client_id)
    if client is None or client.filial_id not in filial_ids or CLIENT not in client.roles:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    return client


def update_client(db: Session, client: Person, data: ClientUpdate) -> Person:
    if data.cpf is not None and client.person_type == PersonType.COMPANY:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="This client is a company (CNPJ) — edit it through /people")
    if data.cpf is not None and data.cpf != client.cpf and _cpf_taken(db, client.filial_id, data.cpf, client.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_DUPLICATE_CPF)

    for field in ("first_name", "last_name", "email", "cpf", "birth_date", "phone"):
        value = getattr(data, field)
        if value is not None:
            setattr(client, field, value)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_DUPLICATE_CPF) from exc
    db.refresh(client)
    return client


def deactivate_client(db: Session, client: Person) -> Person:
    client.status = RecordStatus.INACTIVE
    db.commit()
    db.refresh(client)
    return client


# --- addresses ---


def list_addresses(db: Session, client_id: int) -> list[PersonAddress]:
    return list(
        db.execute(select(PersonAddress).where(PersonAddress.person_id == client_id)).scalars().all()
    )


def get_address_or_404(db: Session, client_id: int, address_id: int) -> PersonAddress:
    address = db.get(PersonAddress, address_id)
    if address is None or address.person_id != client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Address not found")
    return address


def create_address(db: Session, client_id: int, data: ClientAddressCreate) -> PersonAddress:
    address = PersonAddress(person_id=client_id, **data.model_dump())
    db.add(address)
    db.commit()
    db.refresh(address)
    return address


def update_address(db: Session, address: PersonAddress, data: ClientAddressUpdate) -> PersonAddress:
    for field, value in data.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(address, field, value)
    db.commit()
    db.refresh(address)
    return address


def delete_address(db: Session, address: PersonAddress) -> None:
    # Hard delete is fine here — unlike products, addresses aren't referenced by any
    # historical record (orders don't point at a shipping address in this domain).
    db.delete(address)
    db.commit()
