"""Business-timezone helpers (settings.app_timezone, America/Sao_Paulo by default).

Timestamps are stored and returned in UTC — they are instants. What depends on the
business timezone is only how they're *read*: what "today" is, where a day starts and
ends in a report, which hour/weekday a sale belongs to, and which price is valid today.
Never use date.today() or UTC day boundaries for those: the server runs in UTC (Docker),
so from 21:00 in Brasília it would already be "tomorrow".
"""

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.core.config import get_settings


@lru_cache
def local_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().app_timezone)


def local_today() -> date:
    """Today's date in the business timezone — never the server's (UTC in Docker)."""
    return datetime.now(local_tz()).date()


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of a business-timezone day, as aware datetimes for DB filters."""
    start = datetime.combine(day, time.min, tzinfo=local_tz())
    return start, datetime.combine(day + timedelta(days=1), time.min, tzinfo=local_tz())


def to_local(moment: datetime) -> datetime:
    return moment.astimezone(local_tz())
