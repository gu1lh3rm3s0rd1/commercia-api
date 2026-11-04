"""Baseline forecasting models (Fase 8) — plain Python, no numpy/pandas.

Every model is a pure function with the same signature:

    model(values, dates, horizon, **params) -> list[float]   # one prediction per future day

`values` is a daily series aligned with `dates` (consecutive days). A value of None marks
a CENSORED day — the product was out of stock, so what sold that day was capped by the
stock, not by demand. Censored days are skipped by every model: they never pull the
forecast down. Predictions are never negative.

weekday_profile (on by default for the level models): this store sells ~70% more on a
Saturday than on a Sunday, so a flat level would be wrong every day of the week. The
level is computed on de-seasonalized values and multiplied back by each target day's
weekday weight (from the last 8 weeks).
"""

from datetime import date, timedelta

VERSION = "1.0"

PROFILE_WEEKS = 8


def _observed(values: list[float | None]) -> list[tuple[int, float]]:
    return [(i, v) for i, v in enumerate(values) if v is not None]


def weekday_weights(values: list[float | None], dates: list[date]) -> list[float]:
    """Weight of each weekday (Mon..Sun) relative to the average day, over the last 8 weeks
    of uncensored data. 1.0 for a weekday without data, or when nothing sold at all."""
    start = max(0, len(values) - PROFILE_WEEKS * 7)
    sums, counts = [0.0] * 7, [0] * 7
    for i in range(start, len(values)):
        if values[i] is not None:
            wd = dates[i].weekday()
            sums[wd] += values[i]
            counts[wd] += 1
    means = [sums[w] / counts[w] if counts[w] else None for w in range(7)]
    known = [m for m in means if m is not None]
    overall = sum(known) / len(known) if known else 0.0
    if overall <= 0:
        return [1.0] * 7
    return [m / overall if m is not None else 1.0 for m in means]


def _future_dates(dates: list[date], horizon: int) -> list[date]:
    last = dates[-1]
    return [last + timedelta(days=h) for h in range(1, horizon + 1)]


def _level_forecast(level_fn, values, dates, horizon, use_profile: bool) -> list[float]:
    profile = weekday_weights(values, dates) if use_profile else [1.0] * 7
    adjusted = [
        v / profile[d.weekday()] if v is not None and profile[d.weekday()] > 0 else v
        for v, d in zip(values, dates)
    ]
    level = max(0.0, level_fn(adjusted))
    return [level * profile[d.weekday()] for d in _future_dates(dates, horizon)]


def moving_average(values, dates, horizon, window: int = 28, weekday_profile: bool = True) -> list[float]:
    """Mean of the last `window` uncensored days."""

    def level(series):
        recent = [v for _, v in _observed(series)][-window:]
        return sum(recent) / len(recent) if recent else 0.0

    return _level_forecast(level, values, dates, horizon, weekday_profile)


def weighted_moving_average(values, dates, horizon, window: int = 28, weekday_profile: bool = True) -> list[float]:
    """Like moving_average, but linearly weighted: the most recent day weighs `window`
    times the oldest one."""

    def level(series):
        recent = [v for _, v in _observed(series)][-window:]
        weights = range(1, len(recent) + 1)
        return sum(w * v for w, v in zip(weights, recent)) / sum(weights) if recent else 0.0

    return _level_forecast(level, values, dates, horizon, weekday_profile)


def exp_smoothing(values, dates, horizon, alpha: float = 0.3, weekday_profile: bool = True) -> list[float]:
    """Simple exponential smoothing: level += alpha * (value - level), skipping censored days."""

    def level(series):
        observed = [v for _, v in _observed(series)]
        if not observed:
            return 0.0
        current = observed[0]
        for v in observed[1:]:
            current += alpha * (v - current)
        return current

    return _level_forecast(level, values, dates, horizon, weekday_profile)


def period_comparison(values, dates, horizon, growth_window: int = 28) -> list[float]:
    """Same weekday one year earlier (364 days — keeps the weekday aligned), scaled by the
    recent growth: last `growth_window` days vs. the same days a year before (only days
    uncensored in both). Captures yearly peaks like December, which the level models
    can't. Without a usable value a year back, falls back to the most recent uncensored
    same weekday (last week, or the week before...)."""
    index = {d: i for i, d in enumerate(dates)}

    recent_sum = previous_sum = 0.0
    for i in range(max(0, len(values) - growth_window), len(values)):
        j = index.get(dates[i] - timedelta(days=364))
        if values[i] is not None and j is not None and values[j] is not None:
            recent_sum += values[i]
            previous_sum += values[j]
    growth = recent_sum / previous_sum if previous_sum > 0 else 1.0
    growth = min(2.0, max(0.5, growth))  # one odd month must not double or halve the forecast

    predictions = []
    for target in _future_dates(dates, horizon):
        j = index.get(target - timedelta(days=364))
        if j is not None and values[j] is not None:
            predictions.append(max(0.0, values[j] * growth))
            continue
        fallback = 0.0
        back = target - timedelta(days=7)
        while back >= dates[0]:
            k = index.get(back)
            if k is not None and values[k] is not None:
                fallback = values[k]
                break
            back -= timedelta(days=7)
        predictions.append(max(0.0, fallback))
    return predictions


MODELS = {
    "moving_average": (moving_average, {"window": 28, "weekday_profile": True}),
    "weighted_moving_average": (weighted_moving_average, {"window": 28, "weekday_profile": True}),
    "exp_smoothing": (exp_smoothing, {"alpha": 0.3, "weekday_profile": True}),
    "period_comparison": (period_comparison, {"growth_window": 28}),
}
