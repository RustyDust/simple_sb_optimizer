"""Tests for the optimizer core (no Home Assistant needed).

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "pv_battery_optimizer"))

from optimizer import (  # noqa: E402
    BatteryParams,
    Mode,
    Slot,
    baseline_cost,
    control_mode,
    optimize,
    simulate_slot,
)

T0 = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
Q = timedelta(minutes=15)

PARAMS = BatteryParams(
    capacity_kwh=20,
    min_soc_kwh=2,
    max_soc_kwh=20,
    max_charge_kw=3.3,
    max_discharge_kw=3.3,
    grid_charge_kw=3.3,
    charge_efficiency=0.95,
    discharge_efficiency=0.95,
    wear_cost_per_kwh=0.03,
)


def make_slots(prices, pv=None, load=None, ev=None, block_ev=False):
    pv = pv or [0.0] * len(prices)
    load = load or [0.25] * len(prices)  # 1 kW
    ev = ev or [0.0] * len(prices)
    return [
        Slot(
            start=T0 + i * Q,
            end=T0 + (i + 1) * Q,
            import_price=prices[i],
            export_price=0.08,
            pv_kwh=pv[i],
            load_kwh=load[i] + ev[i],
            ev_kwh=ev[i],
            allow_discharge=not (block_ev and ev[i] > 0),
        )
        for i in range(len(prices))
    ]


class SimulateTest(unittest.TestCase):
    def test_normal_discharge_limited_by_min_soc(self):
        slot = make_slots([0.3], load=[2.0])[0]
        r = simulate_slot(2.5, Mode.NORMAL, slot, PARAMS)
        self.assertAlmostEqual(r.soc_end, 2.0)
        self.assertAlmostEqual(r.battery_discharge, 0.5 * 0.95)

    def test_charge_respects_power_and_capacity(self):
        slot = make_slots([0.1])[0]
        r = simulate_slot(10, Mode.CHARGE, slot, PARAMS)
        self.assertAlmostEqual(r.battery_charge, 3.3 / 4)
        r = simulate_slot(19.9, Mode.CHARGE, slot, PARAMS)
        self.assertAlmostEqual(r.soc_end, 20.0)

    def test_hold_no_battery_flow(self):
        slot = make_slots([0.3], pv=[1.0])[0]
        r = simulate_slot(10, Mode.HOLD, slot, PARAMS)
        self.assertEqual(r.soc_end, 10)
        self.assertAlmostEqual(r.grid_export, 0.75)


class OptimizeTest(unittest.TestCase):
    def test_charges_at_night_just_enough_for_expensive_evening(self):
        # 6h cheap (0.10), then 4h expensive (0.40) with 1 kW load, then cheap
        # again (0.13 < 0.10/0.95²+wear). Evening needs 4 kWh -> charge ~4.4 kWh.
        prices = [0.10] * 24 + [0.40] * 16 + [0.13] * 16
        slots = make_slots(prices)
        plan = optimize(slots, soc_kwh=2.0, params=PARAMS)
        charged = sum(s.result.battery_charge for s in plan.steps if s.mode is Mode.CHARGE)
        self.assertGreater(charged, 3.5)
        self.assertLess(charged, 6.5)  # nowhere near 18 kWh -> no overcharging
        self.assertTrue(all(s.mode is not Mode.CHARGE for s in plan.steps[24:]))
        self.assertGreater(plan.savings, 0.5)

    def test_no_grid_charge_when_pv_fills_battery(self):
        # cheap night, then a sunny day producing far more than needed,
        # then expensive evening
        prices = [0.10] * 24 + [0.30] * 32 + [0.40] * 16
        pv = [0.0] * 24 + [1.5] * 32 + [0.0] * 16  # 6 kW for 8 h = 48 kWh
        plan = optimize(make_slots(prices, pv=pv), 2.0, PARAMS, terminal_price=0.2)
        self.assertAlmostEqual(plan.grid_charge_kwh, 0.0, places=3)

    def test_no_charge_when_spread_too_small(self):
        # 0.25 -> 0.28 does not cover losses + wear
        prices = [0.25] * 24 + [0.28] * 16
        plan = optimize(make_slots(prices), 2.0, PARAMS, terminal_price=0.25)
        self.assertAlmostEqual(plan.grid_charge_kwh, 0.0, places=3)

    def test_holds_battery_for_expensive_period(self):
        # battery has 6 kWh; cheap afternoon with load, expensive evening
        prices = [0.12] * 16 + [0.45] * 16
        plan = optimize(make_slots(prices), 6.0, PARAMS, terminal_price=0.12)
        # no discharge in the cheap period (hold, maybe a small top-up) ...
        self.assertTrue(all(s.mode is not Mode.NORMAL for s in plan.steps[:16]))
        self.assertTrue(all(s.result.battery_discharge == 0 for s in plan.steps[:16]))
        # ... and the expensive period is fully covered by the battery
        self.assertTrue(all(s.mode is Mode.NORMAL for s in plan.steps[16:]))
        self.assertTrue(all(s.result.grid_import < 1e-9 for s in plan.steps[16:]))

    def test_ev_block_policy_prevents_discharge(self):
        prices = [0.40] * 8
        ev = [2.75] * 8  # 11 kW
        plan = optimize(make_slots(prices, ev=ev, block_ev=True), 15.0, PARAMS, 0.3)
        self.assertTrue(all(s.result.battery_discharge == 0 for s in plan.steps))

    def test_ev_optimize_policy_uses_battery_when_worth_it(self):
        prices = [0.40] * 8
        ev = [2.75] * 8
        plan = optimize(make_slots(prices, ev=ev), 15.0, PARAMS, terminal_price=0.10)
        self.assertGreater(sum(s.result.battery_discharge for s in plan.steps), 1.0)

    def test_never_worse_than_baseline(self):
        prices = [0.20, 0.35, 0.10, 0.50, 0.05, 0.30] * 8
        pv = [0.0, 0.3, 0.6, 0.2, 0.0, 0.1] * 8
        slots = make_slots(prices, pv=pv)
        plan = optimize(slots, 8.0, PARAMS, terminal_price=0.2)
        self.assertLessEqual(plan.cost, baseline_cost(slots, 8.0, PARAMS, 0.2) + 1e-6)



class MinBenefitTest(unittest.TestCase):
    # real plan from 2026-11-20, 21:00-23:45: tiny price differences
    PRICES = [0.408, 0.400, 0.387, 0.377, 0.395, 0.384, 0.383, 0.377,
              0.383, 0.375, 0.368, 0.363]
    LOAD = [0.28] * 4 + [0.31] * 4 + [0.63] * 4

    def plan(self, min_benefit):
        p = dataclasses.replace(PARAMS, min_benefit=min_benefit)
        return optimize(make_slots(self.PRICES, load=self.LOAD), 0.155 * 20, p)

    def test_without_min_benefit_flips_between_hold_and_normal(self):
        modes = [s.mode for s in self.plan(0.0).steps]
        self.assertIn(Mode.HOLD, modes)
        self.assertIn(Mode.NORMAL, modes[modes.index(Mode.HOLD):])

    def test_min_benefit_keeps_normal_for_negligible_gains(self):
        plan = self.plan(0.01)
        self.assertTrue(all(s.mode is Mode.NORMAL for s in plan.steps))

    def test_reported_cost_excludes_penalty(self):
        plan = self.plan(0.01)
        self.assertAlmostEqual(plan.cost, plan.baseline_cost, places=6)

    def test_worthwhile_charging_still_planned(self):
        p = dataclasses.replace(PARAMS, min_benefit=0.01)
        plan = optimize(make_slots([0.10] * 24 + [0.40] * 16 + [0.13] * 16), 2.0, p)
        self.assertGreater(plan.grid_charge_kwh, 4.0)
        self.assertGreater(plan.savings, 0.5)

    def test_worthwhile_hold_still_planned(self):
        p = dataclasses.replace(PARAMS, min_benefit=0.01)
        plan = optimize(make_slots([0.12] * 16 + [0.45] * 16), 6.0, p, terminal_price=0.12)
        self.assertTrue(all(s.result.battery_discharge == 0 for s in plan.steps[:16]))

class ControlModeTest(unittest.TestCase):
    def test_switches_to_hold_when_target_reached(self):
        prices = [0.10] * 24 + [0.40] * 16
        plan = optimize(make_slots(prices), 2.0, PARAMS, terminal_price=0.1)
        step = next(s for s in plan.steps if s.mode is Mode.CHARGE)
        now = step.slot.start + timedelta(minutes=1)
        self.assertIs(control_mode(plan, now, step.soc_start), Mode.CHARGE)
        self.assertIs(control_mode(plan, now, step.soc_end + 0.5), Mode.HOLD)

    def test_none_outside_plan(self):
        plan = optimize(make_slots([0.2] * 4), 5.0, PARAMS)
        self.assertIsNone(control_mode(plan, T0 + timedelta(days=1), 5.0))


if __name__ == "__main__":
    unittest.main()
