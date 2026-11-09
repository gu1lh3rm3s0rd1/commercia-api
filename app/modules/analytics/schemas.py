from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

Level = Literal["product", "category", "department", "filial"]
RankLevel = Literal["product", "category", "department"]
RankMetric = Literal["revenue", "quantity", "margin"]

# Percentages are fractions (0.15 = 15%), null when the base is 0 — formatting is the client's job.


class EntityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    level: Level
    id: int
    name: str


class SalesDailyPoint(BaseModel):
    date: date
    quantity: int
    revenue: Decimal
    cost: Decimal
    margin: Decimal


class SalesDailyRead(BaseModel):
    entity: EntityRead
    start_date: date
    end_date: date
    points: list[SalesDailyPoint]


class PeriodTotals(BaseModel):
    start_date: date
    end_date: date
    days: int
    quantity: int
    revenue: Decimal
    cost: Decimal
    margin: Decimal
    margin_pct: Decimal | None


class SalesChange(BaseModel):
    quantity: Decimal | None
    revenue: Decimal | None
    cost: Decimal | None
    margin: Decimal | None


class SalesCompareRead(BaseModel):
    entity: EntityRead
    current: PeriodTotals
    previous: PeriodTotals
    change_pct: SalesChange


class RankingItem(BaseModel):
    level: RankLevel
    id: int
    name: str
    quantity: int
    revenue: Decimal
    cost: Decimal
    margin: Decimal
    margin_pct: Decimal | None
    share_pct: Decimal | None  # share of the filial total for the ranking metric


class SalesRankingRead(BaseModel):
    level: RankLevel
    metric: RankMetric
    start_date: date
    end_date: date
    items: list[RankingItem]


class StockDailyPoint(BaseModel):
    date: date
    end_of_day_qty: int
    min_qty: int
    was_out_of_stock: bool


class StockDailyRead(BaseModel):
    entity: EntityRead
    start_date: date
    end_date: date
    points: list[StockDailyPoint]


class StockoutItem(BaseModel):
    product_id: int
    product_name: str
    category_id: int
    category_name: str
    days_out: int
    last_out_date: date
    current_qty: int | None


class StockoutsRead(BaseModel):
    start_date: date
    end_date: date
    items: list[StockoutItem]


class JobInfo(BaseModel):
    job: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    from_date: date | None
    to_date: date | None


class AnalyticsStatusRead(BaseModel):
    first_day: date | None
    last_day: date | None  # last day computed (yesterday, once the daily refresh ran)
    last_sale_date: date | None  # last day with a sale — after it there's no data, not zero sales
    last_job: JobInfo | None
    last_successful_job_at: datetime | None
    last_forecast_at: datetime | None
    forecast_data_until: date | None
    last_evaluated_at: datetime | None
