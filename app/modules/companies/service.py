from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import Company


def list_companies(db: Session, accessible: set[int]) -> list[Company]:
    return list(
        db.execute(
            select(Company).where(Company.id.in_(accessible)).order_by(Company.corporate_name)
        )
        .scalars()
        .all()
    )
