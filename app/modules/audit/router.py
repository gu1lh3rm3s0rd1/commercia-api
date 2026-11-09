"""GET /audit-logs — read-only view of the audit trail (RNF11). Requires audit.view.

Visibility (non-superuser): rows of the filiais they can access, plus filial-less rows
(logins, user management) that concern a user linked to one of those filiais. A failed
login with an unknown username belongs to no store — only superusers see those.
"""

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.deps import get_accessible_filial_ids, require_permission
from app.core.timezone import local_day_bounds
from app.db.session import get_db
from app.models.audit import AuditLog
from app.models.user import User, UserCompany
from app.modules.catalog.schemas import Page

router = APIRouter()


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    occurred_at: datetime
    user_id: int | None
    username: str | None
    filial_id: int | None
    action: str
    entity_type: str | None
    entity_id: int | None
    ip: str | None
    details: dict[str, Any]


@router.get("", response_model=Page[AuditLogRead])
def list_audit_logs(
    filial_id: int | None = None,
    user_id: int | None = None,
    action: str | None = Query(None, max_length=60, description='exact ("order.cancel") or a prefix ending in "." ("order.")'),
    entity_type: str | None = Query(None, max_length=40),
    entity_id: int | None = None,
    start_date: date | None = Query(None, description="business-timezone day, inclusive"),
    end_date: date | None = Query(None, description="business-timezone day, inclusive"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    caller: User = Depends(require_permission("audit.view")),
) -> Page[AuditLogRead]:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="start_date must be <= end_date")
    if filial_id is not None and filial_id not in accessible:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this filial")

    conditions = []
    if not caller.is_superuser:
        linked = select(UserCompany.user_id).where(UserCompany.filial_id.in_(accessible))
        conditions.append(or_(
            AuditLog.filial_id.in_(accessible),
            and_(AuditLog.filial_id.is_(None), or_(
                AuditLog.user_id.in_(linked),
                and_(AuditLog.entity_type == "user", AuditLog.entity_id.in_(linked)),
            )),
        ))
    if filial_id is not None:
        conditions.append(AuditLog.filial_id == filial_id)
    if user_id is not None:
        conditions.append(AuditLog.user_id == user_id)
    if action:
        conditions.append(AuditLog.action.startswith(action) if action.endswith(".") else AuditLog.action == action)
    if entity_type:
        conditions.append(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        conditions.append(AuditLog.entity_id == entity_id)
    if start_date:
        conditions.append(AuditLog.occurred_at >= local_day_bounds(start_date)[0])
    if end_date:
        conditions.append(AuditLog.occurred_at < local_day_bounds(end_date)[1])

    base = select(AuditLog).where(*conditions)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(base.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()).limit(limit).offset(offset)).scalars().all()
    return Page(items=[AuditLogRead.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)
