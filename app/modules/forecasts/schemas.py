from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

Level = Literal["product", "category", "department", "filial"]
Target = Literal["quantity", "revenue"]


class ForecastRunRead(BaseModel):
    id: int
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
