from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_scoped_filial_required
from app.db.session import get_db
from app.analytics.queries import resolve_entity
from app.forecasting import evaluation
from app.forecasting import queries as forecast_queries
from app.forecasting.models.baseline import MODELS
from app.models.user import User
from app.modules.forecasts import service
from app.modules.analytics.router import answer
from app.modules.forecasts.schemas import (
    AccuracyRow,
    BacktestRead,
    EntityRef,
    ForecastCompareRead,
    ForecastPoint,
    ForecastRead,
    ForecastRunRead,
    ForecastSummaryRead,
    Kind,
    Level,
    Target,
)

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
    kind: Kind | None = Query("forecast", description="'backtest' lists the retroactive runs instead"),
    limit: int = Query(20, ge=1, le=200),
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[ForecastRunRead]:
    return [ForecastRunRead(**r) for r in service.list_runs(db, filial_id, target, kind, limit)]


@router.get("/accuracy", response_model=list[AccuracyRow])
def get_accuracy(
    target: Target | None = None,
    level: Level | None = "filial",
    kind: Kind | None = Query(None, description="Omit to pool real forecasts and backtests"),
    filial_id: int = Depends(get_scoped_filial_required),
    _user: User = Depends(get_current_user),
) -> list[AccuracyRow]:
    """Read-only report of the persisted evaluations — refreshed by
    'python -m app.workers.cli forecast evaluate', never computed here."""
    return [AccuracyRow(**r) for r in evaluation.accuracy_report(filial_id, target, level, kind)]


def _check_model(model: str | None) -> None:
    if model is not None and model not in MODELS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"model must be one of: {', '.join(MODELS)}")


@router.get("/compare", response_model=ForecastCompareRead)
def compare(
    level: Level = "filial",
    entity_id: int | None = Query(None, description="Not needed for level=filial"),
    target: Target = "quantity",
    model: str | None = Query(None, description="Omit for the most accurate one"),
    history_days: int = Query(28, ge=7, le=366),
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> ForecastCompareRead:
    """History x forecast in one call — the panel's main chart."""
    _check_model(model)
    entity = answer(resolve_entity, db, filial_id, level, entity_id)
    result = answer(forecast_queries.forecast_compare, db, filial_id, entity, target, model, history_days)
    return ForecastCompareRead(
        entity=EntityRef.model_validate(entity), run=ForecastRunRead(**result["run"]),
        history=result["history"], forecast=result["forecast"],
    )


@router.get("/summary", response_model=ForecastSummaryRead)
def summary(
    level: Level = "filial",
    entity_id: int | None = Query(None, description="Not needed for level=filial"),
    model: str | None = Query(None, description="Omit for the most accurate one"),
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> ForecastSummaryRead:
    """Next 7/14 days forecast vs the last 7/14 real days, with how far off the model
    usually is — the panel's top cards, and the agent's "how much will I sell next week"."""
    _check_model(model)
    entity = answer(resolve_entity, db, filial_id, level, entity_id)
    result = answer(forecast_queries.forecast_summary, db, filial_id, entity, model)
    return ForecastSummaryRead(entity=EntityRef.model_validate(entity), data_until=result["data_until"],
                               targets=result["targets"])


@router.get("/backtest", response_model=BacktestRead)
def backtest(
    level: Level = "filial",
    entity_id: int | None = Query(None, description="Not needed for level=filial"),
    target: Target = "quantity",
    model: str | None = Query(None, description="Omit for every model"),
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> BacktestRead:
    """Predicted vs real at every retroactive cut-off (Fase 9) — where each model hit and missed."""
    _check_model(model)
    entity = answer(resolve_entity, db, filial_id, level, entity_id)
    origins = answer(forecast_queries.backtest_series, db, filial_id, entity, target, model)
    return BacktestRead(entity=EntityRef.model_validate(entity), target=target, origins=origins)
