"""Tests for load forecast and PV correction (no Home Assistant needed)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "pv_battery_optimizer"))

from forecast import (  # noqa: E402
    PvSample,
    energy_in_interval,
    forecast_load,
    pv_correction_factors,
    to_hourly,
)

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)  # Friday


class LoadForecastTest(unittest.TestCase):
    def test_weekday_weekend_split(self):
        history = {}
        for d in range(1, 29):
            for h in range(24):
                ts = NOW - timedelta(days=d) + timedelta(hours=h - 12)
                weekend = ts.astimezone(TZ).weekday() >= 5
                history[ts] = 2.0 if weekend else 0.5
        saturday = NOW + timedelta(days=1)
        monday = NOW + timedelta(days=3)
        fc = forecast_load(history, [saturday, monday], TZ)
        self.assertAlmostEqual(fc[saturday], 2.0)
        self.assertAlmostEqual(fc[monday], 0.5)

    def test_recent_days_weigh_more(self):
        history = {
            NOW - timedelta(days=7): 1.0,
            NOW - timedelta(days=28): 3.0,
        }
        fc = forecast_load(history, [NOW], TZ)
        self.assertLess(fc[NOW], 1.3)

    def test_fallback_without_history(self):
        fc = forecast_load({}, [NOW], TZ, default_kwh=0.4)
        self.assertEqual(fc[NOW], 0.4)


class PvCorrectionTest(unittest.TestCase):
    def test_no_data_means_no_correction(self):
        factors = pv_correction_factors([], NOW, TZ)
        self.assertTrue(all(abs(f - 1.0) < 1e-9 for f in factors.values()))

    def test_learns_systematic_overestimate(self):
        samples = [
            PvSample(NOW - timedelta(days=d, hours=h), 2.0, 1.4)
            for d in range(1, 30)
            for h in range(0, 6)
        ]
        factors = pv_correction_factors(samples, NOW, TZ)
        hour = (NOW - timedelta(hours=2)).astimezone(TZ).hour
        self.assertAlmostEqual(factors[hour], 0.7, delta=0.05)

    def test_factor_clamped(self):
        samples = [PvSample(NOW - timedelta(days=d), 0.1, 50.0) for d in range(1, 30)]
        factors = pv_correction_factors(samples, NOW, TZ)
        self.assertLessEqual(max(factors.values()), 1.6)


class EnergyIntervalTest(unittest.TestCase):
    def test_quarter_hours(self):
        hourly = {NOW: 1.0, NOW + timedelta(hours=1): 2.0}
        self.assertAlmostEqual(energy_in_interval(hourly, NOW, NOW + timedelta(minutes=15)), 0.25)
        self.assertAlmostEqual(
            energy_in_interval(hourly, NOW + timedelta(minutes=45), NOW + timedelta(minutes=75)),
            0.25 + 0.5,
        )

    def test_to_hourly(self):
        pts = {NOW + timedelta(minutes=10): 0.2, NOW + timedelta(minutes=50): 0.3}
        self.assertAlmostEqual(to_hourly(pts)[NOW], 0.5)


if __name__ == "__main__":
    unittest.main()
