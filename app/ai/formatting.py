"""pt-BR formatting of tool results BEFORE they reach the model: the data layer uses
fractions (0.124) and plain decimals; handing the model "12,4%" and "R$ 1.234,56" means it
only has to copy, never convert (a model reading 0.124 may well say "0,12%")."""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal


def _group(integer: str) -> str:
    out = ""
    while len(integer) > 3:
        out = "." + integer[-3:] + out
        integer = integer[:-3]
    return integer + out


def money(value) -> str:
    value = Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if value < 0 else ""
    integer, cents = f"{abs(value):.2f}".split(".")
    return f"{sign}R$ {_group(integer)},{cents}"


def pct(fraction) -> str | None:
    """0.124 -> '12,4%'; None stays None (no base to compare against)."""
    if fraction is None:
        return None
    value = (Decimal(fraction) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    sign = "+" if value > 0 else ""
    return f"{sign}{str(value).replace('.', ',')}%"


def share(fraction) -> str | None:
    """Like pct, without a '+' sign (a share is never a change)."""
    result = pct(fraction)
    return result.lstrip("+") if result else result


def qty(value) -> str:
    value = Decimal(value or 0)
    if value == value.to_integral_value():
        return _group(str(int(value)))
    integer, decimals = f"{value:.1f}".split(".")
    return f"{_group(integer)},{decimals}"


def day(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    return value.strftime("%d/%m/%Y")


def period(start: date, end: date) -> str:
    return day(start) if start == end else f"{day(start)} a {day(end)}"
