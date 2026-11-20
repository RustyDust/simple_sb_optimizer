"""Battery schedule optimizer.

Pure Python, no Home Assistant imports, so it can be unit tested standalone.

Model
-----
The horizon is split into slots (typically 15 min, matching the tariff). For
each slot the battery can be put into one of three evcc battery modes:

* NORMAL - self consumption: battery covers deficits / absorbs PV surplus.
* HOLD   - battery is neither charged nor discharged.
* CHARGE - battery is charged with ``grid_charge_kw`` (from PV first, rest
           from the grid).

A backward dynamic program over a discretized state of charge finds the mode
sequence that minimises

    sum(import * import_price - export * export_price + discharge * wear_cost)
    - value of energy left in the battery at the end of the horizon

The forward pass is done with the exact (continuous) SoC, the grid is only
used to approximate the cost-to-go.

Because surplus PV that cannot be stored is exported at the (low) feed-in
price, the optimizer automatically avoids charging from the grid when the PV
forecast would fill the battery anyway - i.e. it does not "overcharge".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from math import ceil


class Mode(StrEnum):
    """evcc battery modes used by the optimizer."""

    NORMAL = "normal"
    HOLD = "hold"
    CHARGE = "charge"


_TIE_EPS = 1e-6


@dataclass(frozen=True)
class BatteryParams:
    """Static battery parameters. Energies in kWh, powers in kW."""

    capacity_kwh: float
    min_soc_kwh: float
    max_soc_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    grid_charge_kw: float
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    wear_cost_per_kwh: float = 0.0  # per kWh (AC) discharged
    soc_step_kwh: float = 0.05  # DP resolution


@dataclass(frozen=True)
class Slot:
    """One planning interval."""

    start: datetime
    end: datetime
    import_price: float  # per kWh
    export_price: float  # per kWh
    pv_kwh: float
    load_kwh: float  # household load incl. EV
    ev_kwh: float = 0.0  # part of load_kwh caused by EV charging
    allow_discharge: bool = True

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


@dataclass(frozen=True)
class SlotResult:
    soc_end: float
    grid_import: float
    grid_export: float
    battery_charge: float  # AC energy into the battery
    battery_discharge: float  # AC energy out of the battery
    cost: float


@dataclass(frozen=True)
class PlanStep:
    slot: Slot
    mode: Mode
    soc_start: float
    result: SlotResult

    @property
    def soc_end(self) -> float:
        return self.result.soc_end


@dataclass
class Plan:
    steps: list[PlanStep]
    cost: float  # incl. terminal value
    baseline_cost: float  # always NORMAL, incl. terminal value
    terminal_price: float
    params: BatteryParams = field(repr=False)

    @property
    def savings(self) -> float:
        return self.baseline_cost - self.cost

    @property
    def grid_charge_kwh(self) -> float:
        """Energy (AC) charged in CHARGE slots that comes from the grid."""
        total = 0.0
        for step in self.steps:
            if step.mode is Mode.CHARGE:
                surplus = max(0.0, step.slot.pv_kwh - step.slot.load_kwh)
                total += max(0.0, step.result.battery_charge - surplus)
        return total

    def step_at(self, when: datetime) -> PlanStep | None:
        for step in self.steps:
            if step.slot.start <= when < step.slot.end:
                return step
        return None

    def next_charge_block(self, when: datetime) -> list[PlanStep]:
        """Return the current or next contiguous block of CHARGE steps."""
        block: list[PlanStep] = []
        for step in self.steps:
            if step.slot.end <= when:
                continue
            if step.mode is Mode.CHARGE:
                block.append(step)
            elif block:
                break
        return block


def simulate_slot(
    soc: float,
    mode: Mode,
    slot: Slot,
    p: BatteryParams,
    charge_cap_kwh: float | None = None,
) -> SlotResult:
    """Simulate one slot with the given mode starting at ``soc`` (kWh).

    ``charge_cap_kwh`` limits the AC energy charged in CHARGE mode, i.e. the
    slot is only partially used for charging. The controller implements this
    by switching to HOLD once the planned SoC is reached.
    """
    h = slot.hours
    net = slot.load_kwh - slot.pv_kwh  # > 0: deficit, < 0: surplus
    room = max(0.0, p.max_soc_kwh - soc) / p.charge_efficiency  # AC energy storable
    avail = max(0.0, soc - p.min_soc_kwh) * p.discharge_efficiency  # AC deliverable

    charge = 0.0
    discharge = 0.0
    if mode is Mode.NORMAL:
        if net > 0:
            if slot.allow_discharge:
                discharge = min(net, p.max_discharge_kw * h, avail)
        else:
            charge = min(-net, p.max_charge_kw * h, room)
    elif mode is Mode.CHARGE:
        charge = min(p.grid_charge_kw * h, room)
        if charge_cap_kwh is not None:
            charge = min(charge, charge_cap_kwh)
    # HOLD: no battery flow

    soc_end = soc + charge * p.charge_efficiency - discharge / p.discharge_efficiency
    grid = net + charge - discharge
    grid_import = max(grid, 0.0)
    grid_export = max(-grid, 0.0)
    cost = (
        grid_import * slot.import_price
        - grid_export * slot.export_price
        + discharge * p.wear_cost_per_kwh
    )
    return SlotResult(soc_end, grid_import, grid_export, charge, discharge, cost)


def _terminal_value(soc: float, p: BatteryParams, price: float) -> float:
    """Negative cost (= value) of energy left in the battery."""
    return -max(0.0, soc - p.min_soc_kwh) * p.discharge_efficiency * price


def _interp(values: list[float], soc: float, step: float) -> float:
    pos = soc / step
    i = int(pos)
    if i >= len(values) - 1:
        return values[-1]
    if i < 0:
        return values[0]
    frac = pos - i
    return values[i] * (1 - frac) + values[i + 1] * frac


def _actions(slot: Slot, p: BatteryParams) -> list[tuple[Mode, float | None]]:
    """Candidate actions for a slot. CHARGE is offered with partial amounts in
    steps matching the SoC resolution, so the plan can stop charging exactly
    when enough energy is stored instead of always charging whole slots."""
    full = p.grid_charge_kw * slot.hours
    n = max(1, ceil(full * p.charge_efficiency / p.soc_step_kwh))
    actions: list[tuple[Mode, float | None]] = [(Mode.NORMAL, None), (Mode.HOLD, None)]
    actions.extend((Mode.CHARGE, full * k / n) for k in range(1, n + 1))
    return actions


def _best_action(
    soc: float,
    slot: Slot,
    actions: list[tuple[Mode, float | None]],
    nxt: list[float],
    p: BatteryParams,
) -> tuple[float, Mode, SlotResult]:
    best_c = float("inf")
    best: tuple[Mode, SlotResult] | None = None
    for mode, cap in actions:
        r = simulate_slot(soc, mode, slot, p, cap)
        c = r.cost + _interp(nxt, r.soc_end, p.soc_step_kwh)
        if c < best_c - _TIE_EPS:
            best_c, best = c, (mode, r)
    assert best is not None
    return best_c, best[0], best[1]


def optimize(
    slots: list[Slot],
    soc_kwh: float,
    params: BatteryParams,
    terminal_price: float | None = None,
) -> Plan:
    """Compute the cost optimal mode schedule for ``slots``."""
    if not slots:
        raise ValueError("no slots to optimize")
    if terminal_price is None:
        # Conservative: energy left at the end is only worth the cheapest
        # price seen in the horizon. Anything higher makes "charge now, value
        # it later" look profitable and the battery gets filled for no
        # concrete reason (overcharging).
        terminal_price = min(s.import_price for s in slots)

    p = params
    step = p.soc_step_kwh
    n_states = int(round(p.capacity_kwh / step)) + 1
    grid = [i * step for i in range(n_states)]
    n = len(slots)

    # value[t][i]: minimal cost from slot t on, starting with soc grid[i]
    value: list[list[float]] = [[0.0] * n_states for _ in range(n + 1)]
    value[n] = [_terminal_value(s, p, terminal_price) for s in grid]

    actions = [_actions(slot, p) for slot in slots]

    for t in range(n - 1, -1, -1):
        nxt = value[t + 1]
        cur = value[t]
        for i, soc in enumerate(grid):
            cur[i] = _best_action(soc, slots[t], actions[t], nxt, p)[0]

    # forward pass with exact soc
    steps: list[PlanStep] = []
    soc = soc_kwh
    total = 0.0
    for t, slot in enumerate(slots):
        _, best_mode, best_r = _best_action(soc, slot, actions[t], value[t + 1], p)
        steps.append(PlanStep(slot, best_mode, soc, best_r))
        total += best_r.cost
        soc = best_r.soc_end
    total += _terminal_value(soc, p, terminal_price)

    return Plan(
        steps=steps,
        cost=total,
        baseline_cost=baseline_cost(slots, soc_kwh, p, terminal_price),
        terminal_price=terminal_price,
        params=p,
    )


def baseline_cost(
    slots: list[Slot], soc_kwh: float, p: BatteryParams, terminal_price: float
) -> float:
    """Cost of plain self consumption (always NORMAL)."""
    soc = soc_kwh
    total = 0.0
    for slot in slots:
        r = simulate_slot(soc, Mode.NORMAL, slot, p)
        total += r.cost
        soc = r.soc_end
    return total + _terminal_value(soc, p, terminal_price)


def control_mode(
    plan: Plan, now: datetime, soc_kwh: float, tolerance_kwh: float = 0.1
) -> Mode | None:
    """Mode to send to evcc right now.

    Returns ``None`` if the plan does not cover ``now`` (stale plan) - the
    caller should then hand control back to evcc.

    In a CHARGE slot the battery is switched to HOLD once the planned SoC for
    the end of the slot is reached, so the battery never gets charged beyond
    the plan even if it charges faster than modeled.
    """
    step = plan.step_at(now)
    if step is None:
        return None
    if step.mode is Mode.CHARGE and soc_kwh >= step.soc_end - tolerance_kwh:
        return Mode.HOLD
    return step.mode
