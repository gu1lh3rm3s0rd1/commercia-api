"""/agent (Fase 12, RF09 / UC012) — the AI agent's alerts and weekly summary. Hulk plan only
(403 "Plano insuficiente" otherwise) and permission agent.view (Administrador, Gerente)."""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent import service
from app.ai.providers import get_provider
from app.ai.providers.base import LLMProvider, LLMUnavailable
from app.core.deps import get_accessible_filial_ids, require_permission
from app.core.plans import AI_ASSISTANT, PlanRequired, require_feature
from app.core.timezone import local_today
from app.db.session import get_db
from app.models.agent import AgentAlert, AgentSummary
from app.models.user import User
from app.modules.catalog.schemas import Page

router = APIRouter()

_viewer = require_permission("agent.view")


class AlertRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filial_id: int
    kind: str
    severity: str
    entity_type: str | None
    entity_id: int | None
    reference_date: date
    title: str
    message: str
    data: dict[str, Any]
    status: str
    handled_by_id: int | None
    handled_at: datetime | None
    created_at: datetime


class AlertUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["new", "seen", "dismissed"]


class SummaryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filial_id: int
    week_start: date
    week_end: date
    content: str
    data: dict[str, Any]
    generated_by: str
    created_at: datetime


class RunResult(BaseModel):
    reference_date: date
    alerts_created: list[AlertRead]
    summary: SummaryRead | None


def provider_or_none() -> LLMProvider | None:
    """The summary falls back to a template, so a missing key / exhausted quota isn't an error here."""
    try:
        return get_provider()
    except LLMUnavailable:
        return None


def _store(filial_id: int, ai_stores: set[int], accessible: set[int]) -> int:
    if filial_id in ai_stores:
        return filial_id
    if filial_id in accessible:
        raise PlanRequired(AI_ASSISTANT)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this filial")


@router.get("/alerts", response_model=Page[AlertRead])
def list_alerts(
    filial_id: int | None = None,
    alert_status: Literal["new", "seen", "dismissed"] | None = Query(None, alias="status"),
    kind: str | None = Query(None, max_length=40),
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _user: User = Depends(_viewer),
    ai_stores: set[int] = Depends(require_feature(AI_ASSISTANT)),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> Page[AlertRead]:
    """Newest first. Without filial_id: every Hulk store of the user."""
    stores = {_store(filial_id, ai_stores, accessible)} if filial_id is not None else ai_stores
    base = select(AgentAlert).where(AgentAlert.filial_id.in_(stores))
    if alert_status:
        base = base.where(AgentAlert.status == alert_status)
    if kind:
        base = base.where(AgentAlert.kind == kind)
    if start_date:
        base = base.where(AgentAlert.reference_date >= start_date)
    if end_date:
        base = base.where(AgentAlert.reference_date <= end_date)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(base.order_by(AgentAlert.reference_date.desc(), AgentAlert.id.desc())
                      .limit(limit).offset(offset)).scalars().all()
    return Page(items=[AlertRead.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.patch("/alerts/{alert_id}", response_model=AlertRead)
def update_alert(
    alert_id: int,
    data: AlertUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(_viewer),
    ai_stores: set[int] = Depends(require_feature(AI_ASSISTANT)),
) -> AlertRead:
    """Mark as seen or dismissed (or back to new)."""
    alert = db.get(AgentAlert, alert_id)
    if alert is None or alert.filial_id not in ai_stores:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    alert.status = data.status
    alert.handled_by_id = user.id if data.status != "new" else None
    alert.handled_at = datetime.now(timezone.utc) if data.status != "new" else None
    db.commit()
    db.refresh(alert)
    return AlertRead.model_validate(alert)


@router.get("/summary", response_model=SummaryRead)
def latest_summary(
    filial_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(_viewer),
    ai_stores: set[int] = Depends(require_feature(AI_ASSISTANT)),
    accessible: set[int] = Depends(get_accessible_filial_ids),
) -> SummaryRead:
    store = _store(filial_id, ai_stores, accessible)
    summary = db.execute(select(AgentSummary).where(AgentSummary.filial_id == store)
                         .order_by(AgentSummary.week_start.desc()).limit(1)).scalar_one_or_none()
    if summary is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No weekly summary yet — it's written every Monday (or via POST /agent/run)")
    return SummaryRead.model_validate(summary)


@router.post("/run", response_model=RunResult)
def run_agent(
    filial_id: int,
    day: date | None = Query(None, description="day to analyse (default: yesterday)"),
    summary: bool = Query(True, description="also (re)write the last week's summary"),
    db: Session = Depends(get_db),
    _user: User = Depends(_viewer),
    ai_stores: set[int] = Depends(require_feature(AI_ASSISTANT)),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    provider: LLMProvider | None = Depends(provider_or_none),
) -> RunResult:
    """Runs the agent now (what the nightly job does) — for the demo. Returns only the alerts
    that are new; a finding already recorded for that day isn't repeated."""
    store = _store(filial_id, ai_stores, accessible)
    reference = day or local_today() - timedelta(days=1)
    created = service.detect(db, store, reference)
    weekly = service.weekly_summary(db, store, provider) if summary else None
    return RunResult(reference_date=reference, alerts_created=[AlertRead.model_validate(a) for a in created],
                     summary=SummaryRead.model_validate(weekly) if weekly else None)
