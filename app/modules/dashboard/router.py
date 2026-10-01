from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import get_accessible_filial_ids, get_current_user, get_scoped_filial_optional
from app.db.session import get_db
from app.models.user import User
from app.modules.catalog.schemas import Page
from app.modules.dashboard import service
from app.modules.dashboard.schemas import (
    ClientsTrendPoint,
    DashboardIndicators,
    HourlySalesPoint,
    InactiveClientItem,
    LowStockItem,
    MarginByProductItem,
    MarginSummaryReport,
    SalesByCategoryItem,
    SalesByClientItem,
    SalesByFilialItem,
    SalesBySellerItem,
    SalesByStatusItem,
    SalesComparisonReport,
    SalesSummaryReport,
    StaleProductItem,
    TopProductItem,
    WeekdaySalesPoint,
)

router = APIRouter()


def _filial_ids(filial_id: int | None, accessible: set[int]) -> set[int]:
    return {filial_id} if filial_id is not None else accessible


@router.get("/indicators", response_model=DashboardIndicators)
def get_indicators(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> DashboardIndicators:
    return service.get_indicators(db, _filial_ids(filial_id, accessible))


# ---------------------------------------------------------------------
# sales reports
# ---------------------------------------------------------------------


@router.get("/reports/sales-summary", response_model=SalesSummaryReport)
def get_sales_summary(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> SalesSummaryReport:
    filial_ids = _filial_ids(filial_id, accessible)
    return service.get_sales_summary(db, filial_ids, start_date, end_date)


@router.get("/reports/sales-by-client", response_model=Page[SalesByClientItem])
def get_sales_by_client(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> Page[SalesByClientItem]:
    filial_ids = _filial_ids(filial_id, accessible)
    items, total = service.get_sales_by_client(db, filial_ids, start_date, end_date, limit, offset)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/reports/sales-by-status", response_model=Page[SalesByStatusItem])
def get_sales_by_status(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> Page[SalesByStatusItem]:
    filial_ids = _filial_ids(filial_id, accessible)
    items, total = service.get_sales_by_status(db, filial_ids, start_date, end_date, limit, offset)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/reports/sales-by-category", response_model=Page[SalesByCategoryItem])
def get_sales_by_category(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> Page[SalesByCategoryItem]:
    filial_ids = _filial_ids(filial_id, accessible)
    items, total = service.get_sales_by_category(db, filial_ids, start_date, end_date, limit, offset)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/reports/top-products", response_model=Page[TopProductItem])
def get_top_products(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    sort_by: Literal["quantity", "revenue"] = Query("revenue"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> Page[TopProductItem]:
    filial_ids = _filial_ids(filial_id, accessible)
    items, total = service.get_top_products(db, filial_ids, start_date, end_date, sort_by, limit, offset)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/reports/sales-comparison", response_model=SalesComparisonReport)
def get_sales_comparison(
    start_date: date = Query(...),
    end_date: date = Query(...),
    filial_id: int | None = Depends(get_scoped_filial_optional),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> SalesComparisonReport:
    filial_ids = _filial_ids(filial_id, accessible)
    return service.get_sales_comparison(db, filial_ids, start_date, end_date)


@router.get("/reports/sales-by-hour", response_model=list[HourlySalesPoint])
def get_sales_by_hour(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> list[HourlySalesPoint]:
    filial_ids = _filial_ids(filial_id, accessible)
    return service.get_sales_by_hour(db, filial_ids, start_date, end_date)


@router.get("/reports/sales-by-weekday", response_model=list[WeekdaySalesPoint])
def get_sales_by_weekday(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> list[WeekdaySalesPoint]:
    filial_ids = _filial_ids(filial_id, accessible)
    return service.get_sales_by_weekday(db, filial_ids, start_date, end_date)


@router.get("/reports/sales-by-seller", response_model=Page[SalesBySellerItem])
def get_sales_by_seller(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> Page[SalesBySellerItem]:
    filial_ids = _filial_ids(filial_id, accessible)
    items, total = service.get_sales_by_seller(db, filial_ids, start_date, end_date, limit, offset)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/reports/sales-by-filial", response_model=Page[SalesByFilialItem])
def get_sales_by_filial(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> Page[SalesByFilialItem]:
    # Deliberately no filial_id scoping param — this report's purpose is comparing
    # across every filial the user can access, not looking at just one.
    items, total = service.get_sales_by_filial(db, accessible, start_date, end_date, limit, offset)
    return Page(items=items, total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------------
# margin reports
# ---------------------------------------------------------------------


@router.get("/reports/margin-summary", response_model=MarginSummaryReport)
def get_margin_summary(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> MarginSummaryReport:
    filial_ids = _filial_ids(filial_id, accessible)
    return service.get_margin_summary(db, filial_ids, start_date, end_date)


@router.get("/reports/margin-by-product", response_model=Page[MarginByProductItem])
def get_margin_by_product(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> Page[MarginByProductItem]:
    filial_ids = _filial_ids(filial_id, accessible)
    items, total = service.get_margin_by_product(db, filial_ids, start_date, end_date, limit, offset)
    return Page(items=items, total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------------
# stock reports
# ---------------------------------------------------------------------


@router.get("/reports/low-stock", response_model=Page[LowStockItem])
def get_low_stock(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    threshold: int = Query(10, ge=0),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> Page[LowStockItem]:
    filial_ids = _filial_ids(filial_id, accessible)
    items, total = service.get_low_stock(db, filial_ids, threshold, limit, offset)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/reports/stale-products", response_model=Page[StaleProductItem])
def get_stale_products(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    days: int = Query(30, ge=1),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> Page[StaleProductItem]:
    filial_ids = _filial_ids(filial_id, accessible)
    items, total = service.get_stale_products(db, filial_ids, days, limit, offset)
    return Page(items=items, total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------------
# client reports
# ---------------------------------------------------------------------


@router.get("/reports/new-clients-trend", response_model=list[ClientsTrendPoint])
def get_new_clients_trend(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    days: int = Query(7, ge=1, le=90),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> list[ClientsTrendPoint]:
    filial_ids = _filial_ids(filial_id, accessible)
    return service.get_new_clients_trend(db, filial_ids, days)


@router.get("/reports/inactive-clients", response_model=Page[InactiveClientItem])
def get_inactive_clients(
    filial_id: int | None = Depends(get_scoped_filial_optional),
    days: int = Query(60, ge=1),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    _user: User = Depends(get_current_user),
) -> Page[InactiveClientItem]:
    filial_ids = _filial_ids(filial_id, accessible)
    items, total = service.get_inactive_clients(db, filial_ids, days, limit, offset)
    return Page(items=items, total=total, limit=limit, offset=offset)
