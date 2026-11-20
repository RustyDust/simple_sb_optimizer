"""Diagnostics: current plan, saved evening plans and plan vs. reality."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import OptimizerConfigEntry
from .const import CONF_PASSWORD
from .optimizer import plan_rows

TO_REDACT = {CONF_PASSWORD}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OptimizerConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data
    live = coordinator.live
    stored = coordinator.stored

    current: dict[str, Any] | None = None
    if data is not None:
        current = {
            "created": data.created.isoformat(),
            "soc_kwh": round(data.soc_kwh, 3),
            "cost": round(data.plan.cost, 4),
            "baseline_cost": round(data.plan.baseline_cost, 4),
            "terminal_price": data.plan.terminal_price,
            "rows": plan_rows(data.plan, coordinator.params.capacity_kwh),
        }

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "control": {
            "enabled": coordinator.control_enabled,
            "applied_mode": coordinator.applied_mode.value if coordinator.applied_mode else None,
            "desired_mode": (m.value if (m := coordinator.desired_mode()) else None),
        },
        "live": None
        if live is None
        else {
            "soc_percent": live.soc_percent,
            "capacity_kwh": live.capacity_kwh,
            "battery_mode": live.battery_mode,
            "ev_charging": live.ev_charging,
        },
        "pv_correction": {
            "factors": data.pv_factors if data else None,
            "samples": len(stored["samples"]),
            "pending_forecast_snapshots": sorted(stored["snapshots"]),
        },
        "current_plan": current,
        "plan_vs_reality": await coordinator.async_evaluate_plans(),
    }
