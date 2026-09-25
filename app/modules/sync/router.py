from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_scoped_filial_required, require_permission
from app.db.session import get_db
from app.models.user import User
from app.modules.sync import service
from app.modules.sync.schemas import PullResponse, PushRequest, PushResponse

router = APIRouter()

_MANAGE_ORDERS = [Depends(require_permission("orders.manage"))]


@router.post("/push", response_model=PushResponse, dependencies=_MANAGE_ORDERS)
def push(
    data: PushRequest,
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PushResponse:
    results = service.push_batch(db, filial_id, user.id, data.sales)
    return PushResponse(results=results)


@router.get("/pull", response_model=PullResponse)
def pull(
    filial_id: int = Depends(get_scoped_filial_required),
    last_synced_at: datetime | None = Query(
        None, description="Omit on first sync to receive the full catalog."
    ),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> PullResponse:
    return service.pull_changes(db, filial_id, last_synced_at)
