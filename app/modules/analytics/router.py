"""Read-only analytics routes for the panel — thin wrappers over app/analytics/queries.py,
the same functions the AI agent will call (Fase 11)."""

from collections.abc import Callable
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.analytics import queries
from app.core.deps import get_current_user, get_scoped_filial_required
from app.db.session import get_db
from app.models.user import User
from app.modules.analytics.schemas import (
    AnalyticsStatusRead,
    EntityRead,
    Level,
    RankLevel,
    RankMetric,
    SalesCompareRead,
    SalesDailyRead,
    SalesRankingRead,
    StockDailyRead,
    StockoutsRead,
)

router = APIRouter()

# filial_id is required (unlike the dashboard): analytics series are per filial, and the
# entity checks below need to know which one.

_START = Query(None, description="Default: 28 days before end_date")
_END = Query(None, description="Default: the filial's last day with a sale")


def answer(fn: Callable, *args, **kwargs):
    """Maps the query layer's errors to HTTP — 404 for an unknown entity / missing data,
    422 for an invalid period or parameter."""
    try:
        return fn(*args, **kwargs)
    except queries.InvalidPeriod as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except LookupError as exc:  # EntityNotFound, and NoForecast for the forecast routes
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/sales/daily", response_model=SalesDailyRead)
def sales_daily(
    level: Level = "filial",
    entity_id: int | None = Query(None, description="Not needed for level=filial"),
    start_date: date | None = _START,
    end_date: date | None = _END,
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> SalesDailyRead:
    entity = answer(queries.resolve_entity, db, filial_id, level, entity_id)
    start, end = answer(queries.resolve_period, db, filial_id, start_date, end_date)
    points = queries.sales_daily(db, filial_id, entity, start, end)
    return SalesDailyRead(entity=EntityRead.model_validate(entity), start_date=start, end_date=end, points=points)


@router.get("/sales/ranking", response_model=SalesRankingRead)
def sales_ranking(
    level: RankLevel = "product",
    metric: RankMetric = "revenue",
    limit: int = Query(10, ge=1, le=100),
    start_date: date | None = _START,
    end_date: date | None = _END,
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> SalesRankingRead:
    start, end = answer(queries.resolve_period, db, filial_id, start_date, end_date)
    items = answer(queries.sales_ranking, db, filial_id, level, start, end, metric, limit)
    return SalesRankingRead(level=level, metric=metric, start_date=start, end_date=end, items=items)


@router.get("/sales/compare", response_model=SalesCompareRead)
def sales_compare(
    level: Level = "filial",
    entity_id: int | None = None,
    start_date: date | None = _START,
    end_date: date | None = _END,
    compare_start_date: date | None = Query(None, description="Default: the period right before, same length"),
    compare_end_date: date | None = None,
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> SalesCompareRead:
    entity = answer(queries.resolve_entity, db, filial_id, level, entity_id)
    start, end = answer(queries.resolve_period, db, filial_id, start_date, end_date)
    if (compare_start_date is None) != (compare_end_date is None):
        raise HTTPException(422, detail="pass both compare dates or neither")
    if compare_start_date is not None:
        answer(queries.resolve_period, db, filial_id, compare_start_date, compare_end_date)
    result = queries.sales_compare(db, filial_id, entity, start, end, compare_start_date, compare_end_date)
    return SalesCompareRead(entity=EntityRead.model_validate(entity), **{k: v for k, v in result.items()})


@router.get("/stock/daily", response_model=StockDailyRead)
def stock_daily(
    product_id: int,
    start_date: date | None = _START,
    end_date: date | None = _END,
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> StockDailyRead:
    product = answer(queries.resolve_entity, db, filial_id, "product", product_id)
    start, end = answer(queries.resolve_period, db, filial_id, start_date, end_date)
    points = queries.stock_daily(db, filial_id, product, start, end)
    return StockDailyRead(entity=EntityRead.model_validate(product), start_date=start, end_date=end, points=points)


@router.get("/stockouts", response_model=StockoutsRead)
def stockouts(
    limit: int = Query(20, ge=1, le=100),
    start_date: date | None = _START,
    end_date: date | None = _END,
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> StockoutsRead:
    start, end = answer(queries.resolve_period, db, filial_id, start_date, end_date)
    return StockoutsRead(start_date=start, end_date=end, items=queries.stockouts(db, filial_id, start, end, limit))


@router.get("/status", response_model=AnalyticsStatusRead)
def analytics_status(
    filial_id: int = Depends(get_scoped_filial_required),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> AnalyticsStatusRead:
    return AnalyticsStatusRead(**queries.status(db, filial_id))
