"""Weather based PV model trained on the system's own production history.

Pure Python, no Home Assistant imports.

Idea: for a fixed position of the sun the output of any PV system (any
orientation, several arrays, shading by trees or buildings) is close to

    pv = a * DNI + b * DHI

where DNI is the direct normal and DHI the diffuse horizontal irradiance -
the geometry is constant, so the plane-of-array irradiance is linear in both.
``a`` and ``b`` depend on the sun position only. They are estimated for every
target hour by a weighted least squares fit over all historical hours, with
weights falling off with the angular distance between the sun positions
(kernel regression) and with the age of the sample.

Inverter clipping is handled by capping at the highest production seen.
Effects that are not a function of the sun position (snow, soiling) only enter
as averages.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
import math

HOUR = timedelta(hours=1)
J2000 = 946728000.0  # 2000-01-01 12:00 UTC as unix timestamp


@dataclass(frozen=True)
class WeatherHour:
    """Mean irradiance (W/m²) over one hour."""

    ghi: float
    dni: float
    dhi: float


Vector = tuple[float, float, float]


def sun_vector(ts: datetime, lat: float, lon: float) -> Vector:
    """Unit vector pointing at the sun as (east, north, up).

    Low precision solar position (about 0.01°), plenty for this purpose."""
    d = (ts.timestamp() - J2000) / 86400
    g = math.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    ecl_lon = math.radians(q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    obliquity = math.radians(23.439 - 0.00000036 * d)
    ra = math.atan2(math.cos(obliquity) * math.sin(ecl_lon), math.cos(ecl_lon))
    dec = math.asin(math.sin(obliquity) * math.sin(ecl_lon))
    gmst_hours = (18.697374558 + 24.06570982441908 * d) % 24
    ha = math.radians(gmst_hours * 15 + lon) - ra  # hour angle, positive = west
    phi = math.radians(lat)
    up = math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(ha)
    east = -math.cos(dec) * math.sin(ha)
    north = math.sin(dec) * math.cos(phi) - math.cos(dec) * math.cos(ha) * math.sin(phi)
    return (east, north, up)


def sun_elevation(ts: datetime, lat: float, lon: float) -> float:
    """Solar elevation in degrees."""
    return math.degrees(math.asin(max(-1.0, min(1.0, sun_vector(ts, lat, lon)[2]))))


def hour_sun_vector(start: datetime, lat: float, lon: float) -> Vector:
    """Mean sun vector over an hour (four sub-steps, sun below the horizon
    clamped to it), so hours around sunrise and sunset are represented by the
    part of the hour the sun is actually up."""
    acc = [0.0, 0.0, 0.0]
    for minutes in (7.5, 22.5, 37.5, 52.5):
        e, n, u = sun_vector(start + timedelta(minutes=minutes), lat, lon)
        acc[0] += e
        acc[1] += n
        acc[2] += max(u, -0.05)
    return (acc[0] / 4, acc[1] / 4, acc[2] / 4)


@dataclass(frozen=True)
class TrainingPoint:
    start: datetime
    sun: Vector
    ghi: float
    dni: float
    dhi: float
    actual_kwh: float


class PvModel:
    """Kernel weighted local linear regression of PV output on DNI / DHI."""

    def __init__(
        self,
        lat: float,
        lon: float,
        points: Iterable[TrainingPoint],
        bandwidth: float = 0.1,
        half_life_days: float = 365.0,
        min_effective_samples: float = 8.0,
    ) -> None:
        self.lat = lat
        self.lon = lon
        self.bandwidth = bandwidth
        self.half_life_days = half_life_days
        self.min_effective_samples = min_effective_samples
        # sorted by the sun's height for a cheap pre-selection
        self.points = sorted(points, key=lambda p: p.sun[2])
        self._ups = [p.sun[2] for p in self.points]
        self.cap_kwh = max((p.actual_kwh for p in self.points), default=0.0)

    @classmethod
    def build(
        cls,
        lat: float,
        lon: float,
        weather: dict[datetime, WeatherHour],
        actual: dict[datetime, float],
        **kwargs: float,
    ) -> PvModel:
        """Pair weather and production per hour (hours with daylight only)."""
        points = []
        for start, w in weather.items():
            act = actual.get(start)
            if act is None or act < 0:
                continue
            if w.ghi <= 0 and act <= 0.001:
                continue
            points.append(
                TrainingPoint(start, hour_sun_vector(start, lat, lon), w.ghi, w.dni, w.dhi, act)
            )
        return cls(lat, lon, points, **kwargs)

    @property
    def size(self) -> int:
        return len(self.points)

    def predict_hour(
        self, start: datetime, w: WeatherHour, before: datetime | None = None
    ) -> float | None:
        """Expected production (kWh) for the hour starting at ``start``.

        ``before`` restricts training data to hours before that time
        (backtesting). None if there are too few similar hours."""
        sun = hour_sun_vector(start, self.lat, self.lon)
        if w.ghi <= 0 and sun[2] <= 0:
            return 0.0
        h = self.bandwidth
        lo = bisect_left(self._ups, sun[2] - 3 * h)
        hi = bisect_right(self._ups, sun[2] + 3 * h)
        s11 = s12 = s22 = t1 = t2 = sw = sw2 = 0.0
        max_d2 = 9 * h * h
        inv = 1 / (2 * h * h)
        for p in self.points[lo:hi]:
            if before is not None and p.start >= before:
                continue
            d2 = (
                (p.sun[0] - sun[0]) ** 2
                + (p.sun[1] - sun[1]) ** 2
                + (p.sun[2] - sun[2]) ** 2
            )
            if d2 > max_d2:
                continue
            age = abs((start - p.start).total_seconds()) / 86400
            wt = math.exp(-d2 * inv) * 0.5 ** (age / self.half_life_days)
            s11 += wt * p.dni * p.dni
            s12 += wt * p.dni * p.dhi
            s22 += wt * p.dhi * p.dhi
            t1 += wt * p.dni * p.actual_kwh
            t2 += wt * p.dhi * p.actual_kwh
            sw += wt
            sw2 += wt * wt
        if sw2 <= 0 or sw * sw / sw2 < self.min_effective_samples:
            return None
        a, b = _solve_nonneg(s11, s12, s22, t1, t2)
        return min(self.cap_kwh, max(0.0, a * w.dni + b * w.dhi))

    def predict(self, weather: dict[datetime, WeatherHour]) -> dict[datetime, float]:
        """Hourly forecast (kWh); hours the model can't judge are left out."""
        out: dict[datetime, float] = {}
        for start, w in weather.items():
            value = self.predict_hour(start, w)
            if value is not None:
                out[start] = value
        return out

    def backtest(self, since: datetime, tz: tzinfo) -> dict[str, float | int]:
        """Predict every hour since ``since`` from data before its day only
        (like a forecast made the evening before) and compare with reality.

        Note: the weather used here is a short lead time forecast, so real
        day-ahead errors will be somewhat larger."""
        pairs: list[tuple[float, float]] = []
        daily: dict[str, list[float]] = {}
        for p in self.points:
            if p.start < since:
                continue
            local = p.start.astimezone(tz)
            day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
            fc = self.predict_hour(
                p.start,
                WeatherHour(ghi=p.ghi, dni=p.dni, dhi=p.dhi),
                before=day_start,
            )
            if fc is None:
                continue
            pairs.append((fc, p.actual_kwh))
            day = daily.setdefault(local.date().isoformat(), [0.0, 0.0])
            day[0] += fc
            day[1] += p.actual_kwh
        result = forecast_errors(pairs)
        if daily:
            result["days"] = len(daily)
            result["daily_mae_kwh"] = round(
                sum(abs(fc - act) for fc, act in daily.values()) / len(daily), 3
            )
        return result


def _solve_nonneg(
    s11: float, s12: float, s22: float, t1: float, t2: float
) -> tuple[float, float]:
    """Least squares for y = a*x1 + b*x2 with a, b >= 0 (small ridge term)."""
    ridge = 1e-4 * (s11 + s22) + 1e-12
    s11r, s22r = s11 + ridge, s22 + ridge
    det = s11r * s22r - s12 * s12
    if det > 0:
        a = (t1 * s22r - t2 * s12) / det
        b = (t2 * s11r - t1 * s12) / det
        if a >= 0 and b >= 0:
            return a, b
    a_only = max(0.0, t1 / s11r)
    b_only = max(0.0, t2 / s22r)
    # pick the single-variable fit with the lower residual
    err_a = -2 * a_only * t1 + a_only * a_only * s11
    err_b = -2 * b_only * t2 + b_only * b_only * s22
    return (a_only, 0.0) if err_a <= err_b else (0.0, b_only)


def forecast_errors(pairs: Iterable[tuple[float, float]]) -> dict[str, float | int]:
    """Error statistics for (forecast, actual) kWh pairs."""
    n = 0
    sum_fc = sum_act = sum_abs = 0.0
    for fc, act in pairs:
        n += 1
        sum_fc += fc
        sum_act += act
        sum_abs += abs(fc - act)
    if n == 0:
        return {"hours": 0}
    return {
        "hours": n,
        "forecast_kwh": round(sum_fc, 2),
        "actual_kwh": round(sum_act, 2),
        "mae_kwh": round(sum_abs / n, 3),
        "bias_kwh": round((sum_fc - sum_act) / n, 3),
        # relative to the actual energy: 0.2 = errors add up to 20 % of it
        "relative_error": round(sum_abs / sum_act, 3) if sum_act > 0 else None,
    }


def compare_day_ahead(sources: dict[str, list[list]]) -> dict[str, dict]:
    """Compare day-ahead forecast samples ``[ts, forecast, actual]`` of several
    sources on the hours they have in common (and each on all its hours)."""
    by_source = {name: {s[0]: (s[1], s[2]) for s in samples} for name, samples in sources.items()}
    filled = [hours for hours in by_source.values() if hours]
    common = set.intersection(*(set(h) for h in filled)) if len(filled) > 1 else set()
    result: dict[str, dict] = {}
    for name, hours in by_source.items():
        result[name] = {
            "all_hours": forecast_errors(hours.values()),
            "common_hours": forecast_errors(hours[ts] for ts in common if ts in hours),
        }
    return result
