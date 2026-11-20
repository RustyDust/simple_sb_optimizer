"""Load forecast and PV forecast correction.

Pure Python, no Home Assistant imports.

All hourly series are ``dict[datetime, float]`` keyed by the (timezone aware)
start of the hour, values in kWh.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

HOUR = timedelta(hours=1)


def _bucket(local: datetime) -> tuple[int, bool]:
    return local.hour, local.weekday() >= 5


def forecast_load(
    history: dict[datetime, float],
    targets: Iterable[datetime],
    tz: tzinfo,
    half_life_days: float = 7.0,
    default_kwh: float = 0.4,
) -> dict[datetime, float]:
    """Forecast hourly household load.

    For each target hour use an exponentially weighted mean of the same local
    hour on the same kind of day (weekday / weekend) from the history. Falls
    back to the same hour on any day, then to the overall mean.
    """
    by_bucket: dict[tuple[int, bool], list[tuple[datetime, float]]] = defaultdict(list)
    by_hour: dict[int, list[tuple[datetime, float]]] = defaultdict(list)
    for start, kwh in history.items():
        if kwh is None or kwh < 0:
            continue
        local = start.astimezone(tz)
        by_bucket[_bucket(local)].append((start, kwh))
        by_hour[local.hour].append((start, kwh))

    values = [v for v in history.values() if v is not None and v >= 0]
    overall = sum(values) / len(values) if values else default_kwh

    def weighted(samples: list[tuple[datetime, float]], target: datetime) -> float | None:
        num = den = 0.0
        for start, kwh in samples:
            age = (target - start).total_seconds() / 86400
            if age <= 0:
                continue
            w = 0.5 ** (age / half_life_days)
            num += w * kwh
            den += w
        return num / den if den > 0 else None

    result: dict[datetime, float] = {}
    for target in targets:
        local = target.astimezone(tz)
        value = weighted(by_bucket.get(_bucket(local), []), target)
        if value is None:
            value = weighted(by_hour.get(local.hour, []), target)
        result[target] = overall if value is None else value
    return result


@dataclass(frozen=True)
class PvSample:
    """Day-ahead forecast vs. actual production for one hour."""

    start: datetime
    forecast_kwh: float
    actual_kwh: float


def pv_correction_factors(
    samples: Iterable[PvSample],
    now: datetime,
    tz: tzinfo,
    half_life_days: float = 30.0,
    prior_kwh: float = 2.0,
    min_factor: float = 0.3,
    max_factor: float = 1.6,
) -> dict[int, float]:
    """Per local hour-of-day correction factor ``actual / forecast``.

    Uses exponential time decay (adapts to the season) and shrinks each hour
    towards the global factor, which itself is shrunk towards 1.0. With no
    data every factor is 1.0.
    """
    sums: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    g_act = g_fc = 0.0
    for s in samples:
        if s.forecast_kwh < 0.05 and s.actual_kwh < 0.05:
            continue
        age = max(0.0, (now - s.start).total_seconds() / 86400)
        w = 0.5 ** (age / half_life_days)
        hour = s.start.astimezone(tz).hour
        sums[hour][0] += w * s.actual_kwh
        sums[hour][1] += w * s.forecast_kwh
        g_act += w * s.actual_kwh
        g_fc += w * s.forecast_kwh

    global_factor = (g_act + prior_kwh) / (g_fc + prior_kwh)
    factors: dict[int, float] = {}
    for hour in range(24):
        act, fc = sums.get(hour, (0.0, 0.0))
        f = (act + prior_kwh * global_factor) / (fc + prior_kwh)
        factors[hour] = min(max_factor, max(min_factor, f))
    return factors


def apply_correction(
    forecast: dict[datetime, float], factors: dict[int, float], tz: tzinfo
) -> dict[datetime, float]:
    return {
        start: kwh * factors.get(start.astimezone(tz).hour, 1.0)
        for start, kwh in forecast.items()
    }


def energy_in_interval(
    hourly: dict[datetime, float], start: datetime, end: datetime
) -> float:
    """Energy of an hourly series within ``[start, end)``, assuming constant
    power within each hour."""
    total = 0.0
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour < end:
        kwh = hourly.get(hour)
        if kwh:
            overlap = (min(end, hour + HOUR) - max(start, hour)).total_seconds()
            if overlap > 0:
                total += kwh * overlap / 3600
        hour += HOUR
    return total


def to_hourly(points: dict[datetime, float]) -> dict[datetime, float]:
    """Sum arbitrary timestamped energies (e.g. forecast periods) into hour
    buckets (floored to the full hour, in UTC)."""
    out: dict[datetime, float] = defaultdict(float)
    for ts, kwh in points.items():
        out[ts.replace(minute=0, second=0, microsecond=0)] += kwh
    return dict(out)
