from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.deps import (
    get_accessible_filial_ids,
    get_current_user,
    get_scoped_filial_optional,
    get_scoped_filial_required,
    require_permission,
)
from app.db.session import get_db
from app.models.user import User
from app.modules.catalog.schemas import Page
from app.modules.clients import service
from app.modules.clients.schemas import (
    ClientAddressCreate,
    ClientAddressRead,
    ClientAddressUpdate,
    ClientCreate,
    ClientRead,
    ClientUpdate,
)

router = APIRouter()

_EDIT_CLIENTS = [Depends(require_permission("clients.edit"))]


# ---------------------------------------------------------------------
# clients
# ---------------------------------------------------------------------


@router.get("", response_model=Page[ClientRead])
def list_clients(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> Page[ClientRead]:
    filial_ids = {filial_id} if filial_id is not None else accessible
    items, total = service.list_clients(db, filial_ids, limit, offset)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=ClientRead, status_code=status.HTTP_201_CREATED, dependencies=_EDIT_CLIENTS)
def create_client(
    data: ClientCreate,
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ClientRead:
    return service.create_client(db, filial_id, user.id, data)


@router.get("/{client_id}", response_model=ClientRead)
def get_client(
    client_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> ClientRead:
    return service.get_client_or_404(db, client_id, accessible)


@router.patch("/{client_id}", response_model=ClientRead, dependencies=_EDIT_CLIENTS)
def update_client(
    client_id: int,
    data: ClientUpdate,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> ClientRead:
    client = service.get_client_or_404(db, client_id, accessible)
    return service.update_client(db, client, data)


@router.delete("/{client_id}", response_model=ClientRead, dependencies=_EDIT_CLIENTS)
def deactivate_client(
    client_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> ClientRead:
    """'Delete' means status = inactive (soft delete), same convention as products."""
    client = service.get_client_or_404(db, client_id, accessible)
    return service.deactivate_client(db, client)


# ---------------------------------------------------------------------
# client addresses
# ---------------------------------------------------------------------


@router.get("/{client_id}/addresses", response_model=list[ClientAddressRead])
def list_addresses(
    client_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> list[ClientAddressRead]:
    service.get_client_or_404(db, client_id, accessible)
    return service.list_addresses(db, client_id)


@router.post(
    "/{client_id}/addresses",
    response_model=ClientAddressRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=_EDIT_CLIENTS,
)
def create_address(
    client_id: int,
    data: ClientAddressCreate,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> ClientAddressRead:
    service.get_client_or_404(db, client_id, accessible)
    return service.create_address(db, client_id, data)


@router.patch(
    "/{client_id}/addresses/{address_id}", response_model=ClientAddressRead, dependencies=_EDIT_CLIENTS
)
def update_address(
    client_id: int,
    address_id: int,
    data: ClientAddressUpdate,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> ClientAddressRead:
    service.get_client_or_404(db, client_id, accessible)
    address = service.get_address_or_404(db, client_id, address_id)
    return service.update_address(db, address, data)


@router.delete(
    "/{client_id}/addresses/{address_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=_EDIT_CLIENTS
)
def delete_address(
    client_id: int,
    address_id: int,
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> None:
    service.get_client_or_404(db, client_id, accessible)
    address = service.get_address_or_404(db, client_id, address_id)
    service.delete_address(db, address)
