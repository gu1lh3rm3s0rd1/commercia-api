"""Audit trail (RNF11, migration 0010): who did what, when, from where.

Append-only — no route updates or deletes it. Rows are written in the SAME transaction as
the operation they describe (app/core/audit.py), so a failed operation leaves no log and a
committed one always has its log. Lives in `public`: it's a recorded fact, not derived data.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # copied at write time: the log stays readable even if the user is renamed later
    username: Mapped[str | None] = mapped_column(String(60))
    filial_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(60))  # e.g. "order.create", "auth.login_failed"
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    ip: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
