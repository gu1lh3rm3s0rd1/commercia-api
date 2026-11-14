"""Named periods → dates, computed by code (LLMs get calendar arithmetic wrong).
Business timezone; weeks run Monday–Sunday."""

from datetime import date, timedelta
from typing import Literal

from app.core.timezone import local_today

PeriodName = Literal[
    "today", "yesterday", "last_7_days", "last_14_days", "last_28_days", "last_30_days",
    "this_week", "last_week", "this_month", "last_month", "this_year", "last_year", "custom",
]
CompareWith = Literal["previous_period", "same_period_last_year"]


class PeriodError(ValueError):
    pass


def resolve(period: PeriodName, start_date: date | None = None, end_date: date | None = None,
            today: date | None = None) -> tuple[date, date]:
    today = today or local_today()
    if period == "custom":
        if start_date is None or end_date is None:
            raise PeriodError("period 'custom' needs start_date and end_date")
        if start_date > end_date:
            raise PeriodError("start_date must not be after end_date")
        return start_date, end_date
    month_start = today.replace(day=1)
    week_start = today - timedelta(days=today.weekday())
    if period == "today":
        return today, today
    if period == "yesterday":
        return today - timedelta(days=1), today - timedelta(days=1)
    if period.startswith("last_") and period.endswith("_days"):
        days = int(period.split("_")[1])
        return today - timedelta(days=days), today - timedelta(days=1)  # complete days, up to yesterday
    if period == "this_week":
        return week_start, today
    if period == "last_week":
        return week_start - timedelta(days=7), week_start - timedelta(days=1)
    if period == "this_month":
        return month_start, today
    if period == "last_month":
        last_end = month_start - timedelta(days=1)
        return last_end.replace(day=1), last_end
    if period == "this_year":
        return today.replace(month=1, day=1), today
    if period == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    raise PeriodError(f"unknown period '{period}'")


def _minus_one_year(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # 29/02
        return day.replace(year=day.year - 1, day=28)


def comparison(start: date, end: date, compare_with: CompareWith) -> tuple[date, date]:
    if compare_with == "same_period_last_year":
        return _minus_one_year(start), _minus_one_year(end)
    length = (end - start).days + 1
    previous_end = start - timedelta(days=1)
    return previous_end - timedelta(days=length - 1), previous_end
