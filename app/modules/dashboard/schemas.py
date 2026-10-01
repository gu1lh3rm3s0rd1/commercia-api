from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from app.models.enums import OrderStatus, PaymentMethod

# --- indicators ---


class SalesTodayIndicator(BaseModel):
    count: int
    total: Decimal


class DailySalesPoint(BaseModel):
    date: date
    count: int
    total: Decimal


class DashboardIndicators(BaseModel):
    sales_today: SalesTodayIndicator
    new_clients_today: int
    active_products: int
    stock_value: Decimal
    last_7_days: list[DailySalesPoint]


# --- sales reports ---


class PaymentMethodBreakdown(BaseModel):
    payment_method: PaymentMethod
    count: int
    total: Decimal


class SalesSummaryReport(BaseModel):
    period_start: date | None
    period_end: date | None
    total_orders: int
    total_revenue: Decimal
    total_discount: Decimal
    average_ticket: Decimal
    cancelled_orders: int
    cancellation_rate: Decimal
    by_payment_method: list[PaymentMethodBreakdown]


class SalesByClientItem(BaseModel):
    client_id: int
    client_name: str
    total_orders: int
    total_revenue: Decimal
    average_ticket: Decimal


class SalesByStatusItem(BaseModel):
    status: OrderStatus
    count: int
    total: Decimal


class SalesByCategoryItem(BaseModel):
    category_id: int
    category_name: str
    department_id: int
    department_name: str
    quantity_sold: int
    total_revenue: Decimal


class TopProductItem(BaseModel):
    product_id: int
    sku: str
    name: str
    quantity_sold: int
    total_revenue: Decimal
    order_count: int


class PeriodTotals(BaseModel):
    period_start: date
    period_end: date
    total_orders: int
    total_revenue: Decimal


class SalesComparisonReport(BaseModel):
    current: PeriodTotals
    previous: PeriodTotals
    revenue_change_pct: Decimal | None
    orders_change_pct: Decimal | None


class HourlySalesPoint(BaseModel):
    hour: int
    count: int
    total: Decimal


class WeekdaySalesPoint(BaseModel):
    weekday: int
    weekday_name: str
    count: int
    total: Decimal


class SalesBySellerItem(BaseModel):
    user_id: int
    username: str
    total_orders: int
    total_revenue: Decimal


class SalesByFilialItem(BaseModel):
    filial_id: int
    filial_name: str
    total_orders: int
    total_revenue: Decimal


# --- margin reports ---


class MarginSummaryReport(BaseModel):
    period_start: date | None
    period_end: date | None
    total_revenue: Decimal
    total_cost: Decimal
    gross_margin: Decimal
    margin_pct: Decimal


class MarginByProductItem(BaseModel):
    product_id: int
    name: str
    quantity_sold: int
    revenue: Decimal
    cost: Decimal
    margin: Decimal
    margin_pct: Decimal


# --- stock reports ---


class LowStockItem(BaseModel):
    product_id: int
    name: str
    sku: str
    quantity: int


class StaleProductItem(BaseModel):
    product_id: int
    name: str
    sku: str
    last_sale_at: datetime | None
    days_since_last_sale: int | None


# --- client reports ---


class ClientsTrendPoint(BaseModel):
    date: date
    count: int


class InactiveClientItem(BaseModel):
    client_id: int
    client_name: str
    last_order_at: datetime | None
    days_since_last_order: int | None
