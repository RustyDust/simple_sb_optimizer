"""Tests for plan vs. reality evaluation (no Home Assistant needed)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "pv_battery_optimizer"))

from evaluation import evaluate_plan  # noqa: E402

T0 = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
Q = timedelta(minutes=15)
H = timedelta(hours=1)


def rows(start: datetime, n: int, *, soc_step: float = 1.0) -> list[dict]:
    return [
        {
            "start": (start + i * Q).isoformat(),
            "mode": "charge" if i < 4 else "normal",
            "price": 0.2,
            "pv": 0.1,
            "load": 0.3,  # incl. ev
            "ev": 0.05,
            "soc": 50 + soc_step * (i + 1),
            "grid": 0.2,
        }
        for i in range(n)
    ]


class EvaluatePlanTest(unittest.TestCase):
    def test_hourly_aggregation_and_summary(self):
        plan = rows(T0, 8)  # two full hours
        actual_pv = {T0: 0.5, T0 + H: 0.3}
        actual_house = {T0: 1.2, T0 + H: 0.8}
        actual_ev = {T0: 0.2, T0 + H: 0.0}
        soc_log = {T0 + H: 53.0, T0 + 2 * H: 60.0}
        res = evaluate_plan(plan, actual_pv, actual_house, actual_ev, soc_log, now=T0 + 3 * H)

        first = res["hours"][0]
        self.assertEqual(first["modes"], ["charge"])
        self.assertAlmostEqual(first["pv_plan"], 0.4)
        self.assertAlmostEqual(first["house_plan"], 1.0)  # (0.3 - 0.05) * 4
        self.assertAlmostEqual(first["ev_plan"], 0.2)
        self.assertEqual(first["soc_end_plan"], 54.0)
        self.assertEqual(first["soc_end_actual"], 53.0)

        s = res["summary"]
        self.assertEqual(s["hours_evaluated"], 2)
        self.assertAlmostEqual(s["pv_plan"], 0.8)
        self.assertAlmostEqual(s["pv_actual"], 0.8)
        self.assertAlmostEqual(s["house_actual"], 2.0)
        self.assertAlmostEqual(s["soc_error_mean"], (-1.0 + 2.0) / 2)
        self.assertAlmostEqual(s["soc_error_largest"], 2.0)

    def test_future_hours_have_no_actuals(self):
        res = evaluate_plan(rows(T0, 8), {T0: 1.0}, {}, {}, {}, now=T0 + H)
        self.assertIsNotNone(res["hours"][0]["pv_actual"])
        self.assertIsNone(res["hours"][1]["pv_actual"])
        self.assertEqual(res["summary"]["hours_evaluated"], 1)

    def test_partial_first_hour_excluded_from_summary(self):
        # real plans: only the first slot starts "now", the rest are aligned
        # 20:07, 20:15 ... 22:00 -> 20:xx partial, 21:xx full, 22:xx only 15 min
        plan = rows(T0 + Q, 8)
        plan.insert(0, {**plan[0], "start": (T0 + timedelta(minutes=7)).isoformat()})
        actual = {T0: 1.0, T0 + H: 2.0, T0 + 2 * H: 4.0}
        res = evaluate_plan(plan, actual, {}, {}, {}, now=T0 + 3 * H)
        self.assertEqual([h["partial"] for h in res["hours"]], [True, False, True])
        self.assertEqual(res["summary"]["hours_evaluated"], 1)
        self.assertAlmostEqual(res["summary"]["pv_actual"], 2.0)


if __name__ == "__main__":
    unittest.main()
