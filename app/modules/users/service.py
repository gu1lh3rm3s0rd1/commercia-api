from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.user import User, UserCompany


def list_users(db: Session, limit: int, offset: int) -> tuple[list[User], int]:
    base = select(User)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    items = db.execute(base.order_by(User.username).limit(limit).offset(offset)).scalars().all()
    return list(items), total


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
