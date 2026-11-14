"""/people — the general registry: clients, suppliers and whatever role comes next
(Etapa 2 of clients → people). /clients stays as the compatibility view of the clients."""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.deps import (
    get_accessible_filial_ids,
    get_current_user,
    get_scoped_filial_optional,
    get_scoped_filial_required,
)
from app.core.people import PERSON_ROLES
from app.db.session import get_db
from app.models.enums import RecordStatus
from app.models.user import User
from app.modules.catalog.schemas import Page
from app.modules.clients.schemas import ClientAddressCreate, ClientAddressUpdate
from app.modules.people import service
from app.modules.people.schemas import PersonAddressRead, PersonCreate, PersonRead, PersonUpdate

router = APIRouter()


@router.get("/roles")
def list_roles(_user: User = Depends(get_current_user)) -> dict[str, str]:
    """The valid roles and their labels, for the panel's form."""
    return PERSON_ROLES


@router.get("", response_model=Page[PersonRead])
def list_people(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    role: str | None = Query(None, description=f"one of: {', '.join(PERSON_ROLES)}"),
    record_status: RecordStatus | None = Query(None, alias="status"),
    search: str | None = Query(None, min_length=2, max_length=80, description="part of a name, or the first digits of CPF/CNPJ"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> Page[PersonRead]:
    filial_ids = {filial_id} if filial_id is not None else accessible
    items, total = service.list_people(db, filial_ids, role, record_status, search, limit, offset)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=PersonRead, status_code=status.HTTP_201_CREATED)
def create_person(
    data: PersonCreate,
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PersonRead:
    """Only-client people need clients.edit; any other role needs purchases.manage."""
    return service.create_person(db, user, filial_id, data)


@router.get("/{person_id}", response_model=PersonRead)
def get_person(
    person_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> PersonRead:
    return service.get_person_or_404(db, person_id, accessible)


@router.patch("/{person_id}", response_model=PersonRead)
def update_person(
    person_id: int,
    data: PersonUpdate,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    user: User = Depends(get_current_user),
) -> PersonRead:
    person = service.get_person_or_404(db, person_id, accessible)
    return service.update_person(db, user, person, data)


@router.delete("/{person_id}", response_model=PersonRead)
def deactivate_person(
    person_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    user: User = Depends(get_current_user),
) -> PersonRead:
    """Deactivates (status=inactive) — never deletes."""
    person = service.get_person_or_404(db, person_id, accessible)
    return service.deactivate_person(db, user, person)


# --- addresses ---


@router.get("/{person_id}/addresses", response_model=list[PersonAddressRead])
def list_addresses(
    person_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> list[PersonAddressRead]:
    service.get_person_or_404(db, person_id, accessible)
    return service.list_addresses(db, person_id)


@router.post("/{person_id}/addresses", response_model=PersonAddressRead, status_code=status.HTTP_201_CREATED)
def create_address(
    person_id: int,
    data: ClientAddressCreate,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    user: User = Depends(get_current_user),
) -> PersonAddressRead:
    person = service.get_person_or_404(db, person_id, accessible)
    service.ensure_can_edit(db, user, set(person.roles))
    return service.create_address(db, person_id, data)


@router.patch("/{person_id}/addresses/{address_id}", response_model=PersonAddressRead)
def update_address(
    person_id: int,
    address_id: int,
    data: ClientAddressUpdate,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    user: User = Depends(get_current_user),
) -> PersonAddressRead:
    person = service.get_person_or_404(db, person_id, accessible)
    service.ensure_can_edit(db, user, set(person.roles))
    return service.update_address(db, service.get_address_or_404(db, person_id, address_id), data)


@router.delete("/{person_id}/addresses/{address_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_address(
    person_id: int,
    address_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    user: User = Depends(get_current_user),
) -> None:
    person = service.get_person_or_404(db, person_id, accessible)
    service.ensure_can_edit(db, user, set(person.roles))
    service.delete_address(db, service.get_address_or_404(db, person_id, address_id))
