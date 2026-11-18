from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import audit
from app.models.company import Company
from app.models.enums import SubscriptionPlan


def list_companies(db: Session, accessible: set[int]) -> list[Company]:
    return list(
        db.execute(
            select(Company).where(Company.id.in_(accessible)).order_by(Company.corporate_name)
        )
        .scalars()
        .all()
    )

def change_plan(db: Session, filial_id: int, plan: SubscriptionPlan) -> Company:
    company = db.get(Company, filial_id)
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    if company.plan != plan:
        audit.record(db, "company.plan_change", entity_type="company", entity_id=company.id, filial_id=company.id,
                     details={"plan": [company.plan.value, plan.value]})
        company.plan = plan
        db.commit()
        db.refresh(company)
    return company
