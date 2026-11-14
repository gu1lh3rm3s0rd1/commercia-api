"""People registry (Etapa 2). Rules:
- per store: a person belongs to one filial; CPF/CNPJ unique within it (409, with the id of
  the existing person so the panel can offer "add a role to it" instead);
- permissions follow the roles: a person who is ONLY a client needs clients.edit (sellers
  register clients at the counter); any other role (supplier, ...) needs purchases.manage.
  On an edit, both the current and the new roles count — a seller can't turn a client into
  a supplier, nor edit a supplier;
- "delete" = inactive (orders, purchases and the audit trail point at people).
"""

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import has_permission
from app.core.people import CLIENT
from app.models.enums import PersonType, RecordStatus
from app.models.sales import Person, PersonAddress, person_display_name
from app.models.user import User
from app.modules.clients.schemas import ClientAddressCreate, ClientAddressUpdate
from app.modules.people.schemas import PersonCreate, PersonUpdate

_INDIVIDUAL_ONLY = ("cpf", "first_name", "last_name", "birth_date")
_COMPANY_ONLY = ("cnpj", "corporate_name", "trade_name")


def required_permission(roles: set[str]) -> str:
    return "clients.edit" if roles <= {CLIENT} else "purchases.manage"


def ensure_can_edit(db: Session, user: User, roles: set[str]) -> None:
    code = required_permission(roles)
    if not has_permission(db, user, code):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"Missing permission {code} for people with roles {sorted(roles)}")


def list_people(db: Session, filial_ids: set[int], role: str | None, record_status: RecordStatus | None,
                search: str | None, limit: int, offset: int) -> tuple[list[Person], int]:
    base = select(Person).where(Person.filial_id.in_(filial_ids))
    if role:
        base = base.where(Person.roles.contains([role]))
    if record_status:
        base = base.where(Person.status == record_status)
    if search:
        term = f"%{search.strip()}%"
        digits = "".join(ch for ch in search if ch.isdigit())
        conditions = [Person.first_name.ilike(term), Person.last_name.ilike(term), Person.corporate_name.ilike(term),
                      Person.trade_name.ilike(term), (Person.first_name + " " + Person.last_name).ilike(term)]
        if len(digits) >= 3:
            conditions += [Person.cpf.startswith(digits), Person.cnpj.startswith(digits)]
        base = base.where(or_(*conditions))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    items = db.execute(base.order_by(person_display_name(), Person.id).limit(limit).offset(offset)).scalars().all()
    return list(items), total


def get_person_or_404(db: Session, person_id: int, filial_ids: set[int]) -> Person:
    person = db.get(Person, person_id)
    if person is None or person.filial_id not in filial_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person not found")
    return person


def _ensure_document_free(db: Session, filial_id: int, cpf: str | None, cnpj: str | None,
                          exclude_id: int | None = None) -> None:
    for column, value, label in ((Person.cpf, cpf, "CPF"), (Person.cnpj, cnpj, "CNPJ")):
        if value is None:
            continue
        stmt = select(Person.id).where(Person.filial_id == filial_id, column == value)
        if exclude_id is not None:
            stmt = stmt.where(Person.id != exclude_id)
        existing = db.execute(stmt).scalar()
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail=f"{label} already registered in this filial (person {existing})")


def _commit_or_409(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:  # race between the pre-check and the write
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="CPF/CNPJ already registered in this filial") from exc


def create_person(db: Session, user: User, filial_id: int, data: PersonCreate) -> Person:
    ensure_can_edit(db, user, set(data.roles))
    _ensure_document_free(db, filial_id, data.cpf, data.cnpj)
    person = Person(filial_id=filial_id, created_by_id=user.id, **data.model_dump())
    db.add(person)
    _commit_or_409(db)
    db.refresh(person)
    return person


def update_person(db: Session, user: User, person: Person, data: PersonUpdate) -> Person:
    changes = data.model_dump(exclude_unset=True)
    if "roles" in changes and changes["roles"] is None:
        raise HTTPException(status_code=422, detail="roles can't be null — send at least one role")
    new_roles = set(changes.get("roles") or person.roles)
    ensure_can_edit(db, user, set(person.roles) | new_roles)

    not_for_type = _COMPANY_ONLY if person.person_type == PersonType.INDIVIDUAL else _INDIVIDUAL_ONLY
    wrong = [f for f in not_for_type if changes.get(f) is not None]
    if wrong:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"{person.person_type.value} doesn't take: {', '.join(wrong)}")
    required = ("cpf", "first_name", "last_name") if person.person_type == PersonType.INDIVIDUAL else ("cnpj", "corporate_name")
    cleared = [f for f in required if f in changes and changes[f] is None]
    if cleared:
        raise HTTPException(status_code=422, detail=f"can't clear required field(s): {', '.join(cleared)}")

    _ensure_document_free(db, person.filial_id, changes.get("cpf"), changes.get("cnpj"), exclude_id=person.id)
    for field, value in changes.items():
        if field == "roles":
            value = sorted(new_roles)
        setattr(person, field, value)
    _commit_or_409(db)
    db.refresh(person)
    return person


def deactivate_person(db: Session, user: User, person: Person) -> Person:
    ensure_can_edit(db, user, set(person.roles))
    if person.status != RecordStatus.INACTIVE:
        person.status = RecordStatus.INACTIVE
        db.commit()
        db.refresh(person)
    return person


# --- addresses ---


def list_addresses(db: Session, person_id: int) -> list[PersonAddress]:
    return list(db.execute(select(PersonAddress).where(PersonAddress.person_id == person_id)
                           .order_by(PersonAddress.id)).scalars().all())


def get_address_or_404(db: Session, person_id: int, address_id: int) -> PersonAddress:
    address = db.get(PersonAddress, address_id)
    if address is None or address.person_id != person_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Address not found")
    return address


def create_address(db: Session, person_id: int, data: ClientAddressCreate) -> PersonAddress:
    address = PersonAddress(person_id=person_id, **data.model_dump())
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
    db.delete(address)
    db.commit()
