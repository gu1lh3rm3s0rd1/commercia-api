"""Chatbot pieces that need no database and no network (Fase 11)."""

from datetime import date
from decimal import Decimal

import pytest

from app.ai import formatting as fmt
from app.ai import orchestrator, tools
from app.ai.periods import PeriodError, comparison, resolve
from app.ai.providers.base import ModelTurn, ToolCall
from app.ai.providers.fake import FakeProvider

TODAY = date(2026, 11, 14)  # a Saturday


@pytest.mark.parametrize("period,expected", [
    ("today", (date(2026, 11, 14), date(2026, 11, 14))),
    ("yesterday", (date(2026, 11, 13), date(2026, 11, 13))),
    ("last_7_days", (date(2026, 11, 7), date(2026, 11, 13))),
    ("this_week", (date(2026, 11, 9), date(2026, 11, 14))),
    ("last_week", (date(2026, 11, 2), date(2026, 11, 8))),
    ("this_month", (date(2026, 11, 1), date(2026, 11, 14))),
    ("last_month", (date(2026, 10, 1), date(2026, 10, 31))),
    ("last_year", (date(2025, 1, 1), date(2025, 12, 31))),
])
def test_named_periods(period, expected):
    assert resolve(period, today=TODAY) == expected


def test_custom_period_and_comparisons():
    assert resolve("custom", date(2026, 3, 1), date(2026, 3, 31), today=TODAY) == (date(2026, 3, 1), date(2026, 3, 31))
    with pytest.raises(PeriodError):
        resolve("custom", today=TODAY)
    with pytest.raises(PeriodError):
        resolve("custom", date(2026, 3, 2), date(2026, 3, 1), today=TODAY)
    assert comparison(date(2026, 11, 2), date(2026, 11, 8), "previous_period") == (date(2026, 10, 26), date(2026, 11, 1))
    assert comparison(date(2024, 2, 29), date(2024, 3, 1), "same_period_last_year") == (date(2023, 2, 28), date(2023, 3, 1))


def test_ptbr_formatting():
    assert fmt.money(Decimal("1234567.891")) == "R$ 1.234.567,89"
    assert fmt.money(Decimal("-5")) == "-R$ 5,00"
    assert fmt.pct(Decimal("0.1239")) == "+12,4%"
    assert fmt.pct(Decimal("-0.05")) == "-5,0%"
    assert fmt.pct(None) is None
    assert fmt.share(Decimal("0.25")) == "25,0%"
    assert fmt.qty(Decimal("12000")) == "12.000"
    assert fmt.period(date(2026, 11, 2), date(2026, 11, 8)) == "02/11/2026 a 08/11/2026"


def test_tool_schemas_are_plain():
    specs = tools.specs()
    assert len(specs) == len(tools.TOOLS) >= 14
    for spec in specs:
        dumped = repr(spec.parameters)
        assert "anyOf" not in dumped and "'title'" not in dumped and "$ref" not in dumped, spec.name
        assert spec.description


class _NoDbContext:
    """Unknown tools / invalid args are rejected before any query runs."""
    db = None


def test_bad_tool_calls_come_back_as_errors_not_exceptions():
    ctx = _NoDbContext()
    assert "desconhecida" in tools.run(ctx, ToolCall("drop_database", {}))["erro"]
    assert "argumentos inválidos" in tools.run(ctx, ToolCall("get_sales_ranking", {"limit": 500}))["erro"]
    assert "argumentos inválidos" in tools.run(ctx, ToolCall("get_today", {"sql": "select 1"}))["erro"]


def test_orchestrator_gives_up_after_max_rounds():
    loop = [ModelTurn(text=None, tool_calls=[ToolCall("nope", {})]) for _ in range(orchestrator.MAX_ROUNDS + 3)]
    provider = FakeProvider(loop)
    result = orchestrator.answer(provider, _NoDbContext(), "sys", [], "pergunta")
    assert result.text == orchestrator.FALLBACK
    assert len(result.tool_runs) == orchestrator.MAX_ROUNDS
