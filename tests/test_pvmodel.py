"""Tests for the weather based PV model (pure Python)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import math
import random
from zoneinfo import ZoneInfo

from custom_components.pv_battery_optimizer.pvmodel import (
    PvModel,
    WeatherHour,
    compare_day_ahead,
    forecast_errors,
    hour_sun_vector,
    sun_elevation,
)

LAT, LON = 48.1, 11.6
TZ = ZoneInfo("Europe/Berlin")


def test_sun_position() -> None:
    # Munich, 21 June: solar noon ~11:14 UTC, elevation ~65.3°
    noon = datetime(2026, 6, 21, 11, 14, tzinfo=UTC)
    assert abs(sun_elevation(noon, LAT, LON) - 65.3) < 0.5
    # 21 December: ~18.5°
    assert abs(sun_elevation(datetime(2026, 12, 21, 11, 18, tzinfo=UTC), LAT, LON) - 18.5) < 0.5
    assert sun_elevation(datetime(2026, 6, 21, 23, 0, tzinfo=UTC), LAT, LON) < 0
    # morning sun is in the east, evening sun in the west
    east, _, _ = hour_sun_vector(datetime(2026, 6, 21, 5, 0, tzinfo=UTC), LAT, LON)
    west, _, _ = hour_sun_vector(datetime(2026, 6, 21, 17, 0, tzinfo=UTC), LAT, LON)
    assert east > 0 > west


def _plane(sun, tilt_deg, azimuth_deg):
    """Cosine of the incidence angle on a plane (azimuth 180 = south)."""
    t, a = math.radians(tilt_deg), math.radians(azimuth_deg)
    normal = (math.sin(t) * math.sin(a), math.sin(t) * math.cos(a), math.cos(t))
    norm = math.sqrt(sum(c * c for c in sun)) or 1
    return max(0.0, sum(n * s for n, s in zip(normal, sun, strict=True)) / norm)


def synthetic_system(start: datetime, w: WeatherHour) -> float:
    """South array 6 kWp / 30°, east array 3 kWp / 20°, the south array is
    shaded in the morning below 15° elevation, inverter clips at 7 kW."""
    sun = hour_sun_vector(start, LAT, LON)
    if sun[2] <= 0:
        return 0.0
    elevation = math.degrees(math.asin(min(1, sun[2] / math.sqrt(sum(c * c for c in sun)))))
    shaded = sun[0] > 0 and elevation < 15
    south = (0 if shaded else w.dni * _plane(sun, 30, 180)) + w.dhi * (1 + math.cos(math.radians(30))) / 2
    east = w.dni * _plane(sun, 20, 90) + w.dhi * (1 + math.cos(math.radians(20))) / 2
    return min(7.0, (6 * south + 3 * east) / 1000 * 0.85)


def random_weather(start: datetime, rng: random.Random) -> WeatherHour:
    up = hour_sun_vector(start, LAT, LON)[2]
    if up <= 0:
        return WeatherHour(0.0, 0.0, 0.0)
    k = rng.random()  # clearness
    dni = 900 * k * k * up**0.3
    dhi = 1000 * up**1.2 * (0.12 + 0.4 * (1 - k)) * (0.5 + 0.5 * k)
    return WeatherHour(ghi=dni * up + dhi, dni=dni, dhi=dhi)


def _series(start: datetime, days: int, rng: random.Random):
    weather, actual = {}, {}
    h = start
    while h < start + timedelta(days=days):
        w = random_weather(h, rng)
        weather[h] = w
        actual[h] = synthetic_system(h, w)
        h += timedelta(hours=1)
    return weather, actual


def test_model_learns_orientation_shading_and_clipping() -> None:
    rng = random.Random(1)
    t0 = datetime(2024, 11, 1, tzinfo=UTC)
    weather, actual = _series(t0, 730, rng)
    model = PvModel.build(LAT, LON, weather, actual)
    assert model.size > 7000
    assert abs(model.cap_kwh - 7.0) < 1e-9

    test_w, test_a = _series(t0 + timedelta(days=730), 30, random.Random(2))
    predicted = model.predict(test_w)
    errors = forecast_errors((predicted[h], test_a[h]) for h in predicted)
    assert errors["hours"] > 300
    assert errors["relative_error"] < 0.08
    assert abs(errors["bias_kwh"]) < 0.05

    # never above the observed maximum
    assert max(predicted.values()) <= 7.0
    # nights are zero
    night = datetime(2026, 11, 15, 1, 0, tzinfo=UTC)
    assert model.predict_hour(night, WeatherHour(0, 0, 0)) == 0.0


def test_model_without_similar_history_returns_none() -> None:
    rng = random.Random(3)
    # only winter data: summer noon sun positions are unknown
    weather, actual = _series(datetime(2025, 12, 1, tzinfo=UTC), 30, rng)
    model = PvModel.build(LAT, LON, weather, actual)
    summer_noon = datetime(2026, 6, 21, 11, 0, tzinfo=UTC)
    assert model.predict_hour(summer_noon, WeatherHour(800, 800, 100)) is None


def test_backtest_uses_only_earlier_data() -> None:
    rng = random.Random(4)
    t0 = datetime(2025, 3, 1, tzinfo=UTC)
    weather, actual = _series(t0, 120, rng)
    model = PvModel.build(LAT, LON, weather, actual)
    result = model.backtest(t0 + timedelta(days=100), TZ)
    assert 15 <= result["days"] <= 20
    assert result["relative_error"] < 0.15
    # first day has no earlier data at all
    first = model.backtest(t0, TZ)
    assert first["days"] < 120


def test_compare_day_ahead() -> None:
    a = [["t1", 1.0, 1.0], ["t2", 2.0, 1.0], ["t3", 1.0, 1.0]]
    b = [["t2", 1.0, 1.0], ["t3", 1.5, 1.0]]
    result = compare_day_ahead({"a": a, "b": b, "empty": []})
    assert result["a"]["all_hours"]["hours"] == 3
    assert result["a"]["common_hours"]["hours"] == 2
    assert result["a"]["common_hours"]["mae_kwh"] == 0.5
    assert result["b"]["common_hours"]["mae_kwh"] == 0.25
    assert result["empty"]["all_hours"] == {"hours": 0}


def test_parse_open_meteo_hourly_shifts_to_period_start() -> None:
    from custom_components.pv_battery_optimizer.open_meteo import parse_hourly

    payload = {
        "hourly": {
            "time": [1717214400, 1717218000, 1717221600],  # 04:00, 05:00, 06:00 UTC
            "shortwave_radiation": [29.0, 75.0, None],
            "direct_normal_irradiance": [48.0, 65.0, 66.3],
            "diffuse_radiation": [25.0, 61.0, 85.0],
        }
    }
    out = parse_hourly(payload)
    assert list(out) == [
        datetime(2024, 6, 1, 3, 0, tzinfo=UTC),  # mean of 03:00-04:00
        datetime(2024, 6, 1, 4, 0, tzinfo=UTC),
    ]
    assert out[datetime(2024, 6, 1, 3, 0, tzinfo=UTC)] == WeatherHour(29.0, 48.0, 25.0)
    assert parse_hourly({}) == {}


def test_combine_pv() -> None:
    from custom_components.pv_battery_optimizer.forecast import combine_pv

    h1, h2, h3 = (datetime(2026, 6, 1, h, tzinfo=UTC) for h in (10, 11, 12))
    fc = {h1: 2.0, h2: 2.0}
    om = {h2: 1.0, h3: 1.0}
    assert combine_pv(fc, om, "forecast") == (fc, "forecast")
    assert combine_pv(fc, om, "open_meteo") == ({h1: 2.0, h2: 1.0, h3: 1.0}, "open_meteo")
    assert combine_pv(fc, om, "average") == ({h1: 2.0, h2: 1.5, h3: 1.0}, "average")
    assert combine_pv(fc, None, "open_meteo") == (fc, "forecast (fallback)")
    assert combine_pv(None, om, "forecast") == (om, "open_meteo (fallback)")
    assert combine_pv({}, None, "forecast") == ({}, "forecast")
