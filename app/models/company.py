from datetime import datetime

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class Company(Base):
    """Each row represents one store/filial. A user can access N companies via UserCompany."""

    __tablename__ = "companies"
    __table_args__ = (CheckConstraint("cnpj ~ '^[0-9]{14}$'", name="ck_companies_cnpj_format"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    filial_code: Mapped[str] = mapped_column(String(20), unique=True)
    corporate_name: Mapped[str] = mapped_column(String(160))
    fantasy_name: Mapped[str | None] = mapped_column(String(160))
    cnpj: Mapped[str] = mapped_column(String(14), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
