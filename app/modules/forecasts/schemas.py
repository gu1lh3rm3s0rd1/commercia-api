from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

Level = Literal["product", "category", "department", "filial"]
Target = Literal["quantity", "revenue"]
Kind = Literal["forecast", "backtest"]
HorizonBucket = Literal["1-7", "8-14", "15+"]


class ForecastRunRead(BaseModel):
    id: int
    kind: Kind  # 'backtest' = retroactive run made for evaluation (Fase 9)
    model: str
    model_version: str
    params: dict
    target: Target
    filial_id: int
    train_start: date
    train_end: date
    horizon_days: int
    created_at: datetime
    n_series: int | None
    # holdout accuracy: {"holdout_days", "product": {mae, wape, bias}, "filial": {...}}
    metrics: dict | None


class ForecastPoint(BaseModel):
    date: date
    predicted: Decimal
    lower: Decimal  # 80% band
    upper: Decimal


class ForecastRead(BaseModel):
    level: Level
    entity_id: int
    run: ForecastRunRead
    points: list[ForecastPoint]


class AccuracyRow(BaseModel):
    """How far off a model is, pooled over every evaluated run (Fase 9). wape is the
    headline: total |error| / total sold (0.15 = off by 15%). mape only at aggregated
    levels; bias > 0 = over-forecasting. Ratios, not percentages."""

    model: str
    model_version: str
    target: Target
    level: Level
    horizon_bucket: HorizonBucket  # days ahead of the training cut-off
    n_runs: int
    n_points: int
    wape: Decimal | None
    mape: Decimal | None
    mae: Decimal
    bias: Decimal | None
    first_train_end: date
    last_train_end: date


class EntityRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    level: Level
    id: int
    name: str


class ActualPoint(BaseModel):
    date: date
    actual: Decimal


class ForecastCompareRead(BaseModel):
    """History x forecast in one payload: the panel's main chart."""

    entity: EntityRef
    run: ForecastRunRead
    history: list[ActualPoint]  # real days up to run.train_end
    forecast: list[ForecastPoint]


class TargetSummary(BaseModel):
    model: str
    model_version: str
    run_id: int
    next_7_days: Decimal
    next_14_days: Decimal
    last_7_days: Decimal
    last_14_days: Decimal
    change_7_days_pct: Decimal | None  # fraction; next 7 vs last 7
    change_14_days_pct: Decimal | None
    expected_wape_days_1_7: Decimal | None  # how far off this model usually is at this level (Fase 9)
    expected_wape_days_8_14: Decimal | None


class ForecastSummaryRead(BaseModel):
    entity: EntityRef
    data_until: date  # the forecast's training cut-off: "next 7 days" start the day after
    targets: dict[Target, TargetSummary]


class BacktestPoint(BaseModel):
    date: date
    predicted: Decimal
    actual: Decimal
    out_of_stock: bool  # product level: the real figure was capped by stock that day


class BacktestOrigin(BaseModel):
    run_id: int
    model: str
    model_version: str
    train_end: date
    wape: Decimal | None  # this entity, this cut-off
    points: list[BacktestPoint]


class BacktestRead(BaseModel):
    entity: EntityRef
    target: Target
    origins: list[BacktestOrigin]
