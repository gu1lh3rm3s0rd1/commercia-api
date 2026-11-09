"""User management (RF01 / UC002).

Scope rules (non-superuser callers):
- they only see and manage users linked to a filial they can access;
- they can only link users to filiais they can access (a target's links to OTHER stores
  are kept untouched when they edit the filial list);
- they can't create, edit or deactivate a superuser (and no route creates superusers);
- they can't deactivate themselves nor drop their own users.manage — a store can't lock
  itself out of user management by accident.
"Delete" = deactivate (is_active=false): orders and audit logs point at the user.
"""

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import audit
from app.core.security import hash_password
from app.models.company import Company
from app.models.user import Group, GroupPermission, Permission, User, UserCompany, UserGroup
from app.modules.users.schemas import GroupBrief, UserCreate, UserRead, UserUpdate


def _visible(accessible: set[int], caller_is_superuser: bool):
    linked = select(UserCompany.user_id).where(UserCompany.filial_id.in_(accessible))
    if caller_is_superuser:
        return or_(User.id.in_(linked), User.is_superuser.is_(True))
    # a superuser linked to the store (the seeded admin is) is still not the store's to see
    return and_(User.id.in_(linked), User.is_superuser.is_(False))


def list_users(
    db: Session, accessible: set[int], caller_is_superuser: bool, limit: int, offset: int
) -> tuple[list[User], int]:
    """Users linked to the filiais the caller can access — never another store's users.
    Superusers (global admins, not tied to a filial) are only listed to another superuser."""
    base = select(User).where(_visible(accessible, caller_is_superuser))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    items = db.execute(base.order_by(User.username, User.id).limit(limit).offset(offset)).scalars().all()
    return list(items), total


def get_user_or_404(db: Session, user_id: int, accessible: set[int], caller: User) -> User:
    user = db.execute(
        select(User).where(User.id == user_id, _visible(accessible, caller.is_superuser))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


def get_accessible_filial_ids_bulk(db: Session, users: list[User]) -> dict[int, list[int]]:
    """Batched version of app.core.deps.get_accessible_filial_ids for a whole page of users
    at once (avoids one query per user): a superuser gets every company id, everyone else
    gets their own user_companies rows."""
    superuser_ids = [u.id for u in users if u.is_superuser]
    regular_ids = [u.id for u in users if not u.is_superuser]

    result: dict[int, list[int]] = {}

    if superuser_ids:
        all_company_ids = sorted(db.execute(select(Company.id)).scalars().all())
        for user_id in superuser_ids:
            result[user_id] = all_company_ids

    if regular_ids:
        rows = db.execute(
            select(UserCompany.user_id, UserCompany.filial_id).where(
                UserCompany.user_id.in_(regular_ids)
            )
        ).all()
        by_user: dict[int, list[int]] = {}
        for user_id, filial_id in rows:
            by_user.setdefault(user_id, []).append(filial_id)
        for user_id in regular_ids:
            result[user_id] = sorted(by_user.get(user_id, []))

    return result


def get_groups_bulk(db: Session, user_ids: list[int]) -> dict[int, list[GroupBrief]]:
    if not user_ids:
        return {}
    rows = db.execute(
        select(UserGroup.user_id, Group.id, Group.name)
        .join(Group, Group.id == UserGroup.group_id)
        .where(UserGroup.user_id.in_(user_ids))
        .order_by(Group.name)
    ).all()
    result: dict[int, list[GroupBrief]] = {uid: [] for uid in user_ids}
    for user_id, group_id, name in rows:
        result[user_id].append(GroupBrief(id=group_id, name=name))
    return result


def to_user_reads(db: Session, users: list[User]) -> list[UserRead]:
    filiais = get_accessible_filial_ids_bulk(db, users)
    groups = get_groups_bulk(db, [u.id for u in users])
    return [
        UserRead(
            id=user.id,
            username=user.username,
            full_name=user.full_name,
            email=user.email,
            password_hash=user.password_hash,
            is_active=user.is_active,
            is_superuser=user.is_superuser,
            created_at=user.created_at,
            last_login=user.last_login,
            accessible_filial_ids=filiais.get(user.id, []),
            groups=groups.get(user.id, []),
        )
        for user in users
    ]


# ---------------------------------------------------------------------
# reference lists for the panel's form
# ---------------------------------------------------------------------


def list_groups(db: Session) -> list[tuple[Group, list[str]]]:
    groups = db.execute(select(Group).order_by(Group.name)).scalars().all()
    rows = db.execute(
        select(GroupPermission.group_id, Permission.code)
        .join(Permission, Permission.id == GroupPermission.permission_id)
        .order_by(Permission.code)
    ).all()
    codes: dict[int, list[str]] = {}
    for group_id, code in rows:
        codes.setdefault(group_id, []).append(code)
    return [(g, codes.get(g.id, [])) for g in groups]


def list_permissions(db: Session) -> list[Permission]:
    return list(db.execute(select(Permission).order_by(Permission.code)).scalars().all())


# ---------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------


def _ensure_can_manage(caller: User, target: User) -> None:
    if target.is_superuser and not caller.is_superuser:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only a superuser can manage a superuser")


def _validated_groups(db: Session, group_ids: list[int]) -> list[int]:
    ids = sorted(set(group_ids))
    found = set(db.execute(select(Group.id).where(Group.id.in_(ids))).scalars().all())
    missing = [gid for gid in ids if gid not in found]
    if missing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Group(s) not found: {missing}")
    return ids


def _validated_filiais(filial_ids: list[int], accessible: set[int]) -> list[int]:
    ids = sorted(set(filial_ids))
    outside = [fid for fid in ids if fid not in accessible]
    if outside:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"No access to filial(s): {outside}")
    return ids


def _groups_grant(db: Session, group_ids: list[int], code: str) -> bool:
    return db.execute(
        select(GroupPermission.id)
        .join(Permission, Permission.id == GroupPermission.permission_id)
        .where(GroupPermission.group_id.in_(group_ids), Permission.code == code)
    ).first() is not None


def _check_unique(db: Session, username: str | None, email: str | None, exclude_id: int | None = None) -> None:
    """UC002 alternative flow a: duplicate → clear 409 (case-insensitive, so 'Ana' and
    'ana' can't coexist as two different logins)."""
    for field, value, label in ((User.username, username, "Username"), (User.email, email, "E-mail")):
        if value is None:
            continue
        stmt = select(User.id).where(func.lower(field) == value.lower())
        if exclude_id is not None:
            stmt = stmt.where(User.id != exclude_id)
        if db.execute(stmt).first() is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{label} already registered")


def _current_links(db: Session, user_id: int) -> tuple[list[int], list[int]]:
    groups = sorted(db.execute(select(UserGroup.group_id).where(UserGroup.user_id == user_id)).scalars().all())
    filiais = sorted(db.execute(select(UserCompany.filial_id).where(UserCompany.user_id == user_id)).scalars().all())
    return groups, filiais


def _replace_links(db: Session, user_id: int, group_ids: list[int] | None, filial_ids: list[int] | None) -> None:
    if group_ids is not None:
        db.query(UserGroup).filter(UserGroup.user_id == user_id).delete(synchronize_session=False)
        db.add_all(UserGroup(user_id=user_id, group_id=gid) for gid in group_ids)
    if filial_ids is not None:
        db.query(UserCompany).filter(UserCompany.user_id == user_id).delete(synchronize_session=False)
        db.add_all(UserCompany(user_id=user_id, filial_id=fid) for fid in filial_ids)


def _commit_or_409(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:  # race between the pre-check and the insert
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username or e-mail already registered") from exc


def create_user(db: Session, caller: User, accessible: set[int], data: UserCreate) -> User:
    group_ids = _validated_groups(db, data.group_ids)
    filial_ids = _validated_filiais(data.filial_ids, accessible)
    _check_unique(db, data.username, data.email)

    user = User(
        username=data.username,
        full_name=data.full_name.strip(),
        email=data.email,
        password_hash=hash_password(data.password),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username or e-mail already registered") from exc
    _replace_links(db, user.id, group_ids, filial_ids)
    audit.record(db, "user.create", entity_type="user", entity_id=user.id, details={
        "username": user.username, "full_name": user.full_name, "email": user.email,
        "group_ids": group_ids, "filial_ids": filial_ids,
    })
    _commit_or_409(db)
    db.refresh(user)
    return user


def update_user(db: Session, caller: User, accessible: set[int], target: User, data: UserUpdate) -> User:
    _ensure_can_manage(caller, target)
    old_groups, old_filiais = _current_links(db, target.id)
    changes: dict[str, list] = {}

    new_groups = _validated_groups(db, data.group_ids) if data.group_ids is not None else None
    if new_groups is not None and target.id == caller.id and not caller.is_superuser:
        if not _groups_grant(db, new_groups, "users.manage"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="You cannot remove your own users.manage permission")

    new_filiais = None
    if data.filial_ids is not None:
        requested = _validated_filiais(data.filial_ids, accessible)
        # links to stores the caller can't see are not theirs to remove
        new_filiais = sorted(set(requested) | {f for f in old_filiais if f not in accessible})

    if data.is_active is False and target.id == caller.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot deactivate yourself")

    if data.email is not None and data.email != target.email:
        _check_unique(db, None, data.email, exclude_id=target.id)
        changes["email"] = [target.email, data.email]
        target.email = data.email
    if data.full_name is not None and data.full_name.strip() != target.full_name:
        changes["full_name"] = [target.full_name, data.full_name.strip()]
        target.full_name = data.full_name.strip()
    if data.is_active is not None and data.is_active != target.is_active:
        changes["is_active"] = [target.is_active, data.is_active]
        target.is_active = data.is_active
    if new_groups is not None and new_groups != old_groups:
        changes["group_ids"] = [old_groups, new_groups]
    else:
        new_groups = None
    if new_filiais is not None and new_filiais != old_filiais:
        changes["filial_ids"] = [old_filiais, new_filiais]
    else:
        new_filiais = None

    if changes:
        _replace_links(db, target.id, new_groups, new_filiais)
        action = "user.update"
        if list(changes) == ["is_active"]:
            action = "user.activate" if target.is_active else "user.deactivate"
        audit.record(db, action, entity_type="user", entity_id=target.id, details=changes)
        _commit_or_409(db)
        db.refresh(target)
    return target


def deactivate_user(db: Session, caller: User, target: User) -> User:
    _ensure_can_manage(caller, target)
    if target.id == caller.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot deactivate yourself")
    if target.is_active:
        target.is_active = False
        audit.record(db, "user.deactivate", entity_type="user", entity_id=target.id)
        db.commit()
        db.refresh(target)
    return target


def reset_password(db: Session, caller: User, target: User, new_password: str) -> None:
    _ensure_can_manage(caller, target)
    target.password_hash = hash_password(new_password)
    audit.record(db, "user.password_reset", entity_type="user", entity_id=target.id)  # never the password
    db.commit()
