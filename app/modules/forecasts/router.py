from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_scoped_filial_required
from app.db.session import get_db
from app.forecasting.models.baseline import MODELS
from app.models.user import User
from app.modules.forecasts import service
from app.modules.forecasts.schemas import ForecastPoint, ForecastRead, ForecastRunRead, Level, Target

router = APIRouter()

# filial_id is required even on these reads (unlike the dashboard): every forecast run
# belongs to exactly one filial, and summing runs across filiais isn't a forecast any
# run produced.


@router.get("", response_model=ForecastRead)
def get_forecast(
    level: Level,
    entity_id: int,
    target: Target = "quantity",
    model: str | None = Query(None, description=f"One of: {', '.join(MODELS)}. Omit for the most accurate one."),
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> ForecastRead:
    if model is not None and model not in MODELS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"model must be one of: {', '.join(MODELS)}")
    run = service.pick_run(db, filial_id, target, model)
    if run is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"No forecast has been generated yet for filial {filial_id} ({target}) — run "
            "'python -m app.workers.cli forecast run'",
        )
    points = service.get_points(db, run["id"], level, entity_id)
    if not points:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"Run {run['id']} has no forecast for {level} {entity_id} (inactive, or no sales in the last 8 weeks)",
        )
    return ForecastRead(
        level=level,
        entity_id=entity_id,
        run=ForecastRunRead(**run),
        points=[ForecastPoint(**p) for p in points],
    )


@router.get("/runs", response_model=list[ForecastRunRead])
def list_runs(
    target: Target | None = None,
    limit: int = Query(20, ge=1, le=200),
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[ForecastRunRead]:
    return [ForecastRunRead(**r) for r in service.list_runs(db, filial_id, target, limit)]
