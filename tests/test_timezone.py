from datetime import date, datetime, timezone

from app.core.timezone import local_day_bounds, to_local


def test_day_bounds_are_brasilia_midnights():
    start, end = local_day_bounds(date(2026, 10, 30))
    assert start.astimezone(timezone.utc) == datetime(2026, 10, 30, 3, 0, tzinfo=timezone.utc)
    assert end.astimezone(timezone.utc) == datetime(2026, 10, 31, 3, 0, tzinfo=timezone.utc)


def test_late_evening_sale_stays_on_its_brasilia_day():
    # 23:30 in Brasília on the 30th is already the 31st in UTC — it must still count as the 30th
    moment = datetime(2026, 10, 31, 2, 30, tzinfo=timezone.utc)
    start, end = local_day_bounds(date(2026, 10, 30))
    assert start <= moment < end
    assert to_local(moment).date() == date(2026, 10, 30)
    assert to_local(moment).hour == 23
