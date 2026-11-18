"""The AI agent's output (Fase 12): alerts found every night and the weekly summary
(migration 0019, Administrador and Gerente — agent.view), and the actions the consultant
proposes in the chat (migration 0020). Hulk plan only."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class AgentAlert(Base):
    __tablename__ = "agent_alerts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    filial_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    kind: Mapped[str] = mapped_column(String(40))  # app/agent/detectors.py KINDS
    severity: Mapped[str] = mapped_column(String(10))  # info | warning | critical
    entity_type: Mapped[str | None] = mapped_column(String(20))  # product | filial | None
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    reference_date: Mapped[date]  # the day the finding is about
    title: Mapped[str] = mapped_column(String(160))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")  # the numbers behind it
    status: Mapped[str] = mapped_column(String(10), server_default="new")  # new | seen | dismissed
    handled_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    handled_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AgentSummary(Base):
    __tablename__ = "agent_summaries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    filial_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    week_start: Mapped[date]
    week_end: Mapped[date]
    content: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    generated_by: Mapped[str] = mapped_column(String(60))  # the model, or "template"
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AgentAction(Base):
    """One action the consultant proposed in a conversation. It runs only when the user
    answers yes (recognised by code), within 15 minutes; app/agent/actions.py."""

    __tablename__ = "agent_actions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("chat_conversations.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    filial_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    action_type: Mapped[str] = mapped_column(String(40))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    summary: Mapped[str] = mapped_column(Text)  # written by code, shown to the user before the yes
    status: Mapped[str] = mapped_column(String(10), server_default="pending")  # pending|executed|declined|expired|failed
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]
