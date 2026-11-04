"""Unit tests for the Fase 8 baseline models — pure functions, no database."""

from datetime import date, timedelta

from app.forecasting.models import baseline

START = date(2025, 1, 6)  # a Monday


def _dates(n: int) -> list[date]:
    return [START + timedelta(days=i) for i in range(n)]


def test_constant_series_is_forecast_as_constant_by_every_model():
    values, dates = [10.0] * 400, _dates(400)
    for name, (fn, params) in baseline.MODELS.items():
        predicted = fn(values, dates, 14, **params)
        assert len(predicted) == 14, name
        assert all(abs(p - 10.0) < 1e-9 for p in predicted), (name, predicted)


def test_censored_days_never_pull_the_forecast_down():
    # stockout days are None: the 0s they'd otherwise be must not lower the level
    values = [10.0] * 21 + [None] * 7
    dates = _dates(28)
    for name in ("moving_average", "weighted_moving_average", "exp_smoothing"):
        fn, params = baseline.MODELS[name]
        assert all(abs(p - 10.0) < 1e-9 for p in fn(values, dates, 7, **params)), name


def test_weekday_profile_puts_saturday_peak_on_saturdays():
    dates = _dates(56)
    values = [20.0 if d.weekday() == 5 else 10.0 for d in dates]
    predicted = baseline.moving_average(values, dates, 7)
    future = [dates[-1] + timedelta(days=h) for h in range(1, 8)]
    saturday = next(p for p, d in zip(predicted, future) if d.weekday() == 5)
    others = [p for p, d in zip(predicted, future) if d.weekday() != 5]
    assert abs(saturday - 20.0) < 1e-9
    assert all(abs(p - 10.0) < 1e-9 for p in others)
    # without the profile the forecast is flat
    flat = baseline.moving_average(values, dates, 7, weekday_profile=False)
    assert max(flat) - min(flat) < 1e-9


def test_weighted_average_and_smoothing_react_faster_to_a_recent_rise():
    dates = _dates(28)
    values = [10.0] * 21 + [20.0] * 7
    ma = baseline.moving_average(values, dates, 1, weekday_profile=False)[0]
    wma = baseline.weighted_moving_average(values, dates, 1, weekday_profile=False)[0]
    ses = baseline.exp_smoothing(values, dates, 1, weekday_profile=False)[0]
    assert 10.0 < ma < wma < 20.0
    assert ma < ses < 20.0


def test_period_comparison_repeats_last_year_scaled_by_growth():
    dates = _dates(400)
    values = [10.0] * 400
    last = dates[-1]
    target = last + timedelta(days=1)
    values[dates.index(target - timedelta(days=364))] = 50.0  # a peak one year before tomorrow
    for i in range(400 - 28, 400):  # recent 28 days sell double what they did a year before
        values[i] = 20.0
    predicted = baseline.period_comparison(values, dates, 1)
    assert abs(predicted[0] - 100.0) < 1e-9  # 50 a year ago x growth 2.0


def test_period_comparison_falls_back_to_last_week_without_a_year_of_history():
    dates = _dates(30)
    values = [float(d.weekday()) for d in dates]  # value = weekday number
    predicted = baseline.period_comparison(values, dates, 7)
    future = [dates[-1] + timedelta(days=h) for h in range(1, 8)]
    assert predicted == [float(d.weekday()) for d in future]


def test_predictions_are_never_negative_and_empty_history_gives_zero():
    dates = _dates(28)
    for name, (fn, params) in baseline.MODELS.items():
        assert fn([None] * 28, dates, 3, **params) == [0.0, 0.0, 0.0], name
