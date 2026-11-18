from datetime import datetime

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.models.enums import SubscriptionPlan, pg_enum


class Company(Base):
    """Each row represents one store/filial. A user can access N companies via UserCompany."""

    __tablename__ = "companies"
    __table_args__ = (CheckConstraint("cnpj ~ '^[0-9]{14}$'", name="ck_companies_cnpj_format"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    filial_code: Mapped[str] = mapped_column(String(20), unique=True)
    corporate_name: Mapped[str] = mapped_column(String(160))
    fantasy_name: Mapped[str | None] = mapped_column(String(160))
    cnpj: Mapped[str] = mapped_column(String(14), unique=True)
    # subscription plan (migration 0018): Hulk unlocks the Assistente IA — app/core/plans.py
    plan: Mapped[SubscriptionPlan] = mapped_column(pg_enum(SubscriptionPlan, "subscription_plan"),
                                                   default=SubscriptionPlan.PRO)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
