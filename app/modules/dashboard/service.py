from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.catalog import Category, Department, Product, ProductCost, Stock
from app.models.company import Company
from app.models.enums import OrderStatus, RecordStatus, StockMovementReason
from app.models.sales import Client, Order, OrderItem, StockMovement
from app.models.user import User
from app.modules.dashboard.schemas import (
    ClientsTrendPoint,
    DailySalesPoint,
    DashboardIndicators,
    HourlySalesPoint,
    InactiveClientItem,
    LowStockItem,
    MarginByProductItem,
    MarginSummaryReport,
    PaymentMethodBreakdown,
    PeriodTotals,
    SalesByCategoryItem,
    SalesByClientItem,
    SalesByFilialItem,
    SalesBySellerItem,
    SalesByStatusItem,
    SalesComparisonReport,
    SalesSummaryReport,
    SalesTodayIndicator,
    StaleProductItem,
    TopProductItem,
    WeekdaySalesPoint,
)

# "When did this sale really happen" — occurred_at (set by the Fase 5 mobile sync push)
# if present, otherwise created_at (the regular online-order case, where created_at
# already IS the occurrence time). Used everywhere in this module so an offline sale
# synced today but made yesterday counts as yesterday's sale, not today's.
_EFFECTIVE_TIME = func.coalesce(Order.occurred_at, Order.created_at)

_WEEKDAY_NAMES_PT = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _period_conditions(start_date: date | None, end_date: date | None) -> list:
    conditions = []
    if start_date is not None:
        start_dt, _ = _day_bounds(start_date)
        conditions.append(_EFFECTIVE_TIME >= start_dt)
    if end_date is not None:
        _, end_dt = _day_bounds(end_date)
        conditions.append(_EFFECTIVE_TIME < end_dt)
    return conditions


def _pct_change(before: Decimal, after: Decimal) -> Decimal | None:
    if before == 0:
        return None
    return ((after - before) / before * 100).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------
# indicators (RF06/UC007)
# ---------------------------------------------------------------------


def get_indicators(db: Session, filial_ids: set[int]) -> DashboardIndicators:
    today = datetime.now(timezone.utc).date()
    today_start, today_end = _day_bounds(today)

    sales_count, sales_total = db.execute(
        select(func.count(), func.coalesce(func.sum(Order.total), 0)).where(
            Order.filial_id.in_(filial_ids),
            Order.status != OrderStatus.CANCELLED,
            _EFFECTIVE_TIME >= today_start,
            _EFFECTIVE_TIME < today_end,
        )
    ).one()

    new_clients_today = (
        db.scalar(
            select(func.count()).where(
                Client.filial_id.in_(filial_ids),
                Client.created_at >= today_start,
                Client.created_at < today_end,
            )
        )
        or 0
    )

    active_products = (
        db.scalar(
            select(func.count()).where(
                Product.filial_id.in_(filial_ids),
                Product.status == RecordStatus.ACTIVE,
            )
        )
        or 0
    )

    stock_value = (
        db.scalar(
            select(func.coalesce(func.sum(Stock.quantity * ProductCost.cost_value), 0))
            .select_from(Stock)
            .join(ProductCost, ProductCost.product_id == Stock.product_id, isouter=True)
            .where(Stock.filial_id.in_(filial_ids))
        )
        or Decimal("0")
    )

    week_start = today - timedelta(days=6)
    week_start_dt, _ = _day_bounds(week_start)
    _, week_end_dt = _day_bounds(today)

    rows = db.execute(
        select(_EFFECTIVE_TIME, Order.total).where(
            Order.filial_id.in_(filial_ids),
            Order.status != OrderStatus.CANCELLED,
            _EFFECTIVE_TIME >= week_start_dt,
            _EFFECTIVE_TIME < week_end_dt,
        )
    ).all()

    by_day: dict[date, list[Decimal]] = {}
    for effective_time, total in rows:
        day = effective_time.astimezone(timezone.utc).date()
        by_day.setdefault(day, []).append(total)

    last_7_days = [
        DailySalesPoint(
            date=d,
            count=len(by_day.get(d, [])),
            total=sum(by_day.get(d, []), Decimal("0")),
        )
        for d in (week_start + timedelta(days=offset) for offset in range(7))
    ]

    return DashboardIndicators(
        sales_today=SalesTodayIndicator(count=sales_count, total=sales_total),
        new_clients_today=new_clients_today,
        active_products=active_products,
        stock_value=stock_value,
        last_7_days=last_7_days,
    )


# ---------------------------------------------------------------------
# sales reports (RF07/UC008)
# ---------------------------------------------------------------------


def get_sales_summary(
    db: Session, filial_ids: set[int], start_date: date | None, end_date: date | None
) -> SalesSummaryReport:
    base_conditions = [Order.filial_id.in_(filial_ids), *_period_conditions(start_date, end_date)]
    non_cancelled = [*base_conditions, Order.status != OrderStatus.CANCELLED]

    total_orders, total_revenue, total_discount = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(Order.total), 0),
            func.coalesce(func.sum(Order.discount), 0),
        ).where(*non_cancelled)
    ).one()

    cancelled_orders = (
        db.scalar(
            select(func.count()).where(*base_conditions, Order.status == OrderStatus.CANCELLED)
        )
        or 0
    )

    average_ticket = (
        (total_revenue / total_orders).quantize(Decimal("0.01")) if total_orders else Decimal("0.00")
    )
    denominator = total_orders + cancelled_orders
    cancellation_rate = (
        (Decimal(cancelled_orders) / denominator * 100).quantize(Decimal("0.01"))
        if denominator
        else Decimal("0.00")
    )

    breakdown_rows = db.execute(
        select(Order.payment_method, func.count(), func.coalesce(func.sum(Order.total), 0))
        .where(*non_cancelled)
        .group_by(Order.payment_method)
    ).all()
    by_payment_method = [
        PaymentMethodBreakdown(payment_method=payment_method, count=count, total=total)
        for payment_method, count, total in breakdown_rows
    ]

    return SalesSummaryReport(
        period_start=start_date,
        period_end=end_date,
        total_orders=total_orders,
        total_revenue=total_revenue,
        total_discount=total_discount,
        average_ticket=average_ticket,
        cancelled_orders=cancelled_orders,
        cancellation_rate=cancellation_rate,
        by_payment_method=by_payment_method,
    )


def get_sales_by_client(
    db: Session,
    filial_ids: set[int],
    start_date: date | None,
    end_date: date | None,
    limit: int,
    offset: int,
) -> tuple[list[SalesByClientItem], int]:
    conditions = [
        Order.filial_id.in_(filial_ids),
        Order.status != OrderStatus.CANCELLED,
        *_period_conditions(start_date, end_date),
    ]

    total_orders_col = func.count(Order.id).label("total_orders")
    total_revenue_col = func.coalesce(func.sum(Order.total), 0).label("total_revenue")

    base = (
        select(Client.id, Client.first_name, Client.last_name, total_orders_col, total_revenue_col)
        .join(Order, Order.client_id == Client.id)
        .where(*conditions)
        .group_by(Client.id)
    )

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    rows = db.execute(base.order_by(total_revenue_col.desc()).limit(limit).offset(offset)).all()

    items = []
    for client_id, first_name, last_name, total_orders, total_revenue in rows:
        average_ticket = (total_revenue / total_orders).quantize(Decimal("0.01")) if total_orders else Decimal("0.00")
        items.append(
            SalesByClientItem(
                client_id=client_id,
                client_name=f"{first_name} {last_name}",
                total_orders=total_orders,
                total_revenue=total_revenue,
                average_ticket=average_ticket,
            )
        )
    return items, total


def get_sales_by_status(
    db: Session,
    filial_ids: set[int],
    start_date: date | None,
    end_date: date | None,
    limit: int,
    offset: int,
) -> tuple[list[SalesByStatusItem], int]:
    conditions = [Order.filial_id.in_(filial_ids), *_period_conditions(start_date, end_date)]

    base = (
        select(Order.status, func.count(), func.coalesce(func.sum(Order.total), 0))
        .where(*conditions)
        .group_by(Order.status)
    )

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    rows = db.execute(base.order_by(Order.status).limit(limit).offset(offset)).all()

    items = [
        SalesByStatusItem(status=status_value, count=count, total=total_value)
        for status_value, count, total_value in rows
    ]
    return items, total


def get_sales_by_category(
    db: Session,
    filial_ids: set[int],
    start_date: date | None,
    end_date: date | None,
    limit: int,
    offset: int,
) -> tuple[list[SalesByCategoryItem], int]:
    conditions = [
        Order.filial_id.in_(filial_ids),
        Order.status != OrderStatus.CANCELLED,
        *_period_conditions(start_date, end_date),
    ]

    quantity_col = func.coalesce(func.sum(OrderItem.quantity), 0).label("quantity_sold")
    revenue_col = func.coalesce(func.sum(OrderItem.total), 0).label("total_revenue")

    base = (
        select(Category.id, Category.name, Department.id, Department.name, quantity_col, revenue_col)
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .join(Category, Category.id == Product.category_id)
        .join(Department, Department.id == Category.department_id)
        .where(*conditions)
        .group_by(Category.id, Department.id)
    )

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(base.order_by(revenue_col.desc()).limit(limit).offset(offset)).all()

    items = [
        SalesByCategoryItem(
            category_id=category_id,
            category_name=category_name,
            department_id=department_id,
            department_name=department_name,
            quantity_sold=quantity_sold,
            total_revenue=total_revenue,
        )
        for category_id, category_name, department_id, department_name, quantity_sold, total_revenue in rows
    ]
    return items, total


def get_top_products(
    db: Session,
    filial_ids: set[int],
    start_date: date | None,
    end_date: date | None,
    sort_by: str,
    limit: int,
    offset: int,
) -> tuple[list[TopProductItem], int]:
    conditions = [
        Order.filial_id.in_(filial_ids),
        Order.status != OrderStatus.CANCELLED,
        *_period_conditions(start_date, end_date),
    ]

    quantity_col = func.coalesce(func.sum(OrderItem.quantity), 0).label("quantity_sold")
    revenue_col = func.coalesce(func.sum(OrderItem.total), 0).label("total_revenue")
    order_count_col = func.count(func.distinct(Order.id)).label("order_count")

    base = (
        select(Product.id, Product.sku, Product.name, quantity_col, revenue_col, order_count_col)
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(*conditions)
        .group_by(Product.id)
    )

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    sort_col = quantity_col if sort_by == "quantity" else revenue_col
    rows = db.execute(base.order_by(sort_col.desc()).limit(limit).offset(offset)).all()

    items = [
        TopProductItem(
            product_id=product_id,
            sku=sku,
            name=name,
            quantity_sold=quantity_sold,
            total_revenue=total_revenue,
            order_count=order_count,
        )
        for product_id, sku, name, quantity_sold, total_revenue, order_count in rows
    ]
    return items, total


def _period_totals(db: Session, filial_ids: set[int], start_date: date, end_date: date) -> PeriodTotals:
    total_orders, total_revenue = db.execute(
        select(func.count(), func.coalesce(func.sum(Order.total), 0)).where(
            Order.filial_id.in_(filial_ids),
            Order.status != OrderStatus.CANCELLED,
            *_period_conditions(start_date, end_date),
        )
    ).one()
    return PeriodTotals(
        period_start=start_date, period_end=end_date, total_orders=total_orders, total_revenue=total_revenue
    )


def get_sales_comparison(
    db: Session, filial_ids: set[int], start_date: date, end_date: date
) -> SalesComparisonReport:
    current = _period_totals(db, filial_ids, start_date, end_date)

    length_days = (end_date - start_date).days + 1
    previous_end = start_date - timedelta(days=1)
    previous_start = previous_end - timedelta(days=length_days - 1)
    previous = _period_totals(db, filial_ids, previous_start, previous_end)

    revenue_change_pct = _pct_change(previous.total_revenue, current.total_revenue)
    orders_change_pct = _pct_change(Decimal(previous.total_orders), Decimal(current.total_orders))

    return SalesComparisonReport(
        current=current,
        previous=previous,
        revenue_change_pct=revenue_change_pct,
        orders_change_pct=orders_change_pct,
    )


def get_sales_by_hour(
    db: Session, filial_ids: set[int], start_date: date | None, end_date: date | None
) -> list[HourlySalesPoint]:
    rows = db.execute(
        select(_EFFECTIVE_TIME, Order.total).where(
            Order.filial_id.in_(filial_ids),
            Order.status != OrderStatus.CANCELLED,
            *_period_conditions(start_date, end_date),
        )
    ).all()

    by_hour: dict[int, list[Decimal]] = {}
    for effective_time, total in rows:
        hour = effective_time.astimezone(timezone.utc).hour
        by_hour.setdefault(hour, []).append(total)

    return [
        HourlySalesPoint(hour=h, count=len(by_hour.get(h, [])), total=sum(by_hour.get(h, []), Decimal("0")))
        for h in range(24)
    ]


def get_sales_by_weekday(
    db: Session, filial_ids: set[int], start_date: date | None, end_date: date | None
) -> list[WeekdaySalesPoint]:
    rows = db.execute(
        select(_EFFECTIVE_TIME, Order.total).where(
            Order.filial_id.in_(filial_ids),
            Order.status != OrderStatus.CANCELLED,
            *_period_conditions(start_date, end_date),
        )
    ).all()

    by_weekday: dict[int, list[Decimal]] = {}
    for effective_time, total in rows:
        weekday = effective_time.astimezone(timezone.utc).weekday()
        by_weekday.setdefault(weekday, []).append(total)

    return [
        WeekdaySalesPoint(
            weekday=w,
            weekday_name=_WEEKDAY_NAMES_PT[w],
            count=len(by_weekday.get(w, [])),
            total=sum(by_weekday.get(w, []), Decimal("0")),
        )
        for w in range(7)
    ]


def get_sales_by_seller(
    db: Session,
    filial_ids: set[int],
    start_date: date | None,
    end_date: date | None,
    limit: int,
    offset: int,
) -> tuple[list[SalesBySellerItem], int]:
    conditions = [
        Order.filial_id.in_(filial_ids),
        Order.status != OrderStatus.CANCELLED,
        Order.created_by_id.is_not(None),
        *_period_conditions(start_date, end_date),
    ]

    total_orders_col = func.count(Order.id).label("total_orders")
    total_revenue_col = func.coalesce(func.sum(Order.total), 0).label("total_revenue")

    base = (
        select(User.id, User.username, total_orders_col, total_revenue_col)
        .select_from(Order)
        .join(User, User.id == Order.created_by_id)
        .where(*conditions)
        .group_by(User.id)
    )

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(base.order_by(total_revenue_col.desc()).limit(limit).offset(offset)).all()

    items = [
        SalesBySellerItem(user_id=user_id, username=username, total_orders=total_orders, total_revenue=total_revenue)
        for user_id, username, total_orders, total_revenue in rows
    ]
    return items, total


def get_sales_by_filial(
    db: Session,
    filial_ids: set[int],
    start_date: date | None,
    end_date: date | None,
    limit: int,
    offset: int,
) -> tuple[list[SalesByFilialItem], int]:
    conditions = [
        Order.filial_id.in_(filial_ids),
        Order.status != OrderStatus.CANCELLED,
        *_period_conditions(start_date, end_date),
    ]

    total_orders_col = func.count(Order.id).label("total_orders")
    total_revenue_col = func.coalesce(func.sum(Order.total), 0).label("total_revenue")

    base = (
        select(Company.id, Company.fantasy_name, Company.corporate_name, total_orders_col, total_revenue_col)
        .select_from(Order)
        .join(Company, Company.id == Order.filial_id)
        .where(*conditions)
        .group_by(Company.id)
    )

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(base.order_by(total_revenue_col.desc()).limit(limit).offset(offset)).all()

    items = [
        SalesByFilialItem(
            filial_id=filial_id,
            filial_name=fantasy_name or corporate_name,
            total_orders=total_orders,
            total_revenue=total_revenue,
        )
        for filial_id, fantasy_name, corporate_name, total_orders, total_revenue in rows
    ]
    return items, total


# ---------------------------------------------------------------------
# margin reports — use cost_at_sale, frozen at sale time (Fase 4)
# ---------------------------------------------------------------------


def get_margin_summary(
    db: Session, filial_ids: set[int], start_date: date | None, end_date: date | None
) -> MarginSummaryReport:
    conditions = [
        Order.filial_id.in_(filial_ids),
        Order.status != OrderStatus.CANCELLED,
        *_period_conditions(start_date, end_date),
    ]

    total_revenue, total_cost = db.execute(
        select(
            func.coalesce(func.sum(OrderItem.total), 0),
            func.coalesce(func.sum(OrderItem.quantity * OrderItem.cost_at_sale), 0),
        )
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(*conditions)
    ).one()

    gross_margin = total_revenue - total_cost
    margin_pct = (gross_margin / total_revenue * 100).quantize(Decimal("0.01")) if total_revenue else Decimal("0.00")

    return MarginSummaryReport(
        period_start=start_date,
        period_end=end_date,
        total_revenue=total_revenue,
        total_cost=total_cost,
        gross_margin=gross_margin,
        margin_pct=margin_pct,
    )


def get_margin_by_product(
    db: Session,
    filial_ids: set[int],
    start_date: date | None,
    end_date: date | None,
    limit: int,
    offset: int,
) -> tuple[list[MarginByProductItem], int]:
    conditions = [
        Order.filial_id.in_(filial_ids),
        Order.status != OrderStatus.CANCELLED,
        *_period_conditions(start_date, end_date),
    ]

    quantity_expr = func.coalesce(func.sum(OrderItem.quantity), 0)
    revenue_expr = func.coalesce(func.sum(OrderItem.total), 0)
    cost_expr = func.coalesce(func.sum(OrderItem.quantity * OrderItem.cost_at_sale), 0)
    margin_expr = revenue_expr - cost_expr

    base = (
        select(
            Product.id,
            Product.name,
            quantity_expr.label("quantity_sold"),
            revenue_expr.label("revenue"),
            cost_expr.label("cost"),
        )
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(*conditions)
        .group_by(Product.id)
    )

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(base.order_by(margin_expr.desc()).limit(limit).offset(offset)).all()

    items = []
    for product_id, name, quantity_sold, revenue, cost in rows:
        margin = revenue - cost
        margin_pct = (margin / revenue * 100).quantize(Decimal("0.01")) if revenue else Decimal("0.00")
        items.append(
            MarginByProductItem(
                product_id=product_id,
                name=name,
                quantity_sold=quantity_sold,
                revenue=revenue,
                cost=cost,
                margin=margin,
                margin_pct=margin_pct,
            )
        )
    return items, total


# ---------------------------------------------------------------------
# stock reports — current state, not period-scoped
# ---------------------------------------------------------------------


def get_low_stock(
    db: Session, filial_ids: set[int], threshold: int, limit: int, offset: int
) -> tuple[list[LowStockItem], int]:
    base = (
        select(Product.id, Product.name, Product.sku, Stock.quantity)
        .select_from(Stock)
        .join(Product, Product.id == Stock.product_id)
        .where(
            Stock.filial_id.in_(filial_ids),
            Stock.quantity <= threshold,
            Product.status == RecordStatus.ACTIVE,
        )
    )

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(base.order_by(Stock.quantity.asc()).limit(limit).offset(offset)).all()

    items = [LowStockItem(product_id=pid, name=name, sku=sku, quantity=qty) for pid, name, sku, qty in rows]
    return items, total


def get_stale_products(
    db: Session, filial_ids: set[int], days: int, limit: int, offset: int
) -> tuple[list[StaleProductItem], int]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    last_sale_subq = (
        select(
            StockMovement.product_id.label("product_id"),
            func.max(StockMovement.occurred_at).label("last_sale_at"),
        )
        .where(StockMovement.reason == StockMovementReason.SALE)
        .group_by(StockMovement.product_id)
        .subquery()
    )

    base = (
        select(Product.id, Product.name, Product.sku, last_sale_subq.c.last_sale_at)
        .select_from(Product)
        .outerjoin(last_sale_subq, last_sale_subq.c.product_id == Product.id)
        .where(
            Product.filial_id.in_(filial_ids),
            Product.status == RecordStatus.ACTIVE,
            or_(last_sale_subq.c.last_sale_at.is_(None), last_sale_subq.c.last_sale_at < cutoff),
        )
    )

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(
        base.order_by(last_sale_subq.c.last_sale_at.asc().nulls_first()).limit(limit).offset(offset)
    ).all()

    now = datetime.now(timezone.utc)
    items = []
    for product_id, name, sku, last_sale_at in rows:
        days_since = (now - last_sale_at).days if last_sale_at is not None else None
        items.append(
            StaleProductItem(
                product_id=product_id, name=name, sku=sku, last_sale_at=last_sale_at, days_since_last_sale=days_since
            )
        )
    return items, total


# ---------------------------------------------------------------------
# client reports
# ---------------------------------------------------------------------


def get_new_clients_trend(db: Session, filial_ids: set[int], days: int) -> list[ClientsTrendPoint]:
    today = datetime.now(timezone.utc).date()
    window_start = today - timedelta(days=days - 1)
    window_start_dt, _ = _day_bounds(window_start)
    _, window_end_dt = _day_bounds(today)

    rows = db.execute(
        select(Client.created_at).where(
            Client.filial_id.in_(filial_ids),
            Client.created_at >= window_start_dt,
            Client.created_at < window_end_dt,
        )
    ).all()

    by_day: dict[date, int] = {}
    for (created_at,) in rows:
        day = created_at.astimezone(timezone.utc).date()
        by_day[day] = by_day.get(day, 0) + 1

    return [
        ClientsTrendPoint(date=d, count=by_day.get(d, 0))
        for d in (window_start + timedelta(days=offset) for offset in range(days))
    ]


def get_inactive_clients(
    db: Session, filial_ids: set[int], days: int, limit: int, offset: int
) -> tuple[list[InactiveClientItem], int]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    last_order_subq = (
        select(
            Order.client_id.label("client_id"),
            func.max(_EFFECTIVE_TIME).label("last_order_at"),
        )
        .where(Order.status != OrderStatus.CANCELLED)
        .group_by(Order.client_id)
        .subquery()
    )

    base = (
        select(Client.id, Client.first_name, Client.last_name, last_order_subq.c.last_order_at)
        .select_from(Client)
        .outerjoin(last_order_subq, last_order_subq.c.client_id == Client.id)
        .where(
            Client.filial_id.in_(filial_ids),
            or_(last_order_subq.c.last_order_at.is_(None), last_order_subq.c.last_order_at < cutoff),
        )
    )

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(
        base.order_by(last_order_subq.c.last_order_at.asc().nulls_first()).limit(limit).offset(offset)
    ).all()

    now = datetime.now(timezone.utc)
    items = []
    for client_id, first_name, last_name, last_order_at in rows:
        days_since = (now - last_order_at).days if last_order_at is not None else None
        items.append(
            InactiveClientItem(
                client_id=client_id,
                client_name=f"{first_name} {last_name}",
                last_order_at=last_order_at,
                days_since_last_order=days_since,
            )
        )
    return items, total
