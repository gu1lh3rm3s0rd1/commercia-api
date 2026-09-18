from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import RecordStatus
from app.models.sales import Client, ClientAddress
from app.modules.clients.schemas import ClientAddressCreate, ClientAddressUpdate, ClientCreate, ClientUpdate

# Uniqueness of cpf/email is GLOBAL (shared across every filial of the network), not
# per-filial — confirmed 2026-09-17: a client is the same real person regardless of which
# store of the network they buy from.


def _duplicate_client(db: Session, cpf: str, email: str, exclude_id: int | None = None) -> Client | None:
    stmt = select(Client).where((Client.cpf == cpf) | (Client.email == email))
    if exclude_id is not None:
        stmt = stmt.where(Client.id != exclude_id)
    return db.execute(stmt).scalars().first()


def list_clients(db: Session, filial_ids: set[int], limit: int, offset: int) -> tuple[list[Client], int]:
    base = select(Client).where(Client.filial_id.in_(filial_ids))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    items = db.execute(base.order_by(Client.first_name, Client.last_name).limit(limit).offset(offset)).scalars().all()
    return list(items), total


def create_client(db: Session, filial_id: int, created_by_id: int, data: ClientCreate) -> Client:
    if _duplicate_client(db, data.cpf, data.email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A client with this CPF or e-mail already exists"
        )

    client = Client(
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
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A client with this CPF or e-mail already exists"
        ) from exc
    db.refresh(client)
    return client


def get_client_or_404(db: Session, client_id: int, filial_ids: set[int]) -> Client:
    client = db.get(Client, client_id)
    if client is None or client.filial_id not in filial_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    return client


def update_client(db: Session, client: Client, data: ClientUpdate) -> Client:
    new_cpf = data.cpf if data.cpf is not None else client.cpf
    new_email = data.email if data.email is not None else client.email
    if (data.cpf is not None or data.email is not None) and _duplicate_client(
        db, new_cpf, new_email, exclude_id=client.id
    ) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A client with this CPF or e-mail already exists"
        )

    for field in ("first_name", "last_name", "email", "cpf", "birth_date", "phone"):
        value = getattr(data, field)
        if value is not None:
            setattr(client, field, value)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A client with this CPF or e-mail already exists"
        ) from exc
    db.refresh(client)
    return client


def deactivate_client(db: Session, client: Client) -> Client:
    client.status = RecordStatus.INACTIVE
    db.commit()
    db.refresh(client)
    return client


# --- addresses ---


def list_addresses(db: Session, client_id: int) -> list[ClientAddress]:
    return list(
        db.execute(select(ClientAddress).where(ClientAddress.client_id == client_id)).scalars().all()
    )


def get_address_or_404(db: Session, client_id: int, address_id: int) -> ClientAddress:
    address = db.get(ClientAddress, address_id)
    if address is None or address.client_id != client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Address not found")
    return address


def create_address(db: Session, client_id: int, data: ClientAddressCreate) -> ClientAddress:
    address = ClientAddress(client_id=client_id, **data.model_dump())
    db.add(address)
    db.commit()
    db.refresh(address)
    return address


def update_address(db: Session, address: ClientAddress, data: ClientAddressUpdate) -> ClientAddress:
    for field, value in data.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(address, field, value)
    db.commit()
    db.refresh(address)
    return address


def delete_address(db: Session, address: ClientAddress) -> None:
    # Hard delete is fine here — unlike products, addresses aren't referenced by any
    # historical record (orders don't point at a shipping address in this domain).
    db.delete(address)
    db.commit()
