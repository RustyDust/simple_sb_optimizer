"""Sensors exposing the optimizer plan."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import OptimizerConfigEntry
from .coordinator import OptimizerCoordinator
from .entity import OptimizerEntity
from .optimizer import Mode, plan_rows


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _charge_target(c: OptimizerCoordinator) -> float | None:
    block = c.data.plan.next_charge_block(dt_util.utcnow())
    if not block:
        return None
    return round(block[-1].soc_end / c.params.capacity_kwh * 100, 1)


def _charge_target_attrs(c: OptimizerCoordinator) -> dict[str, Any]:
    block = c.data.plan.next_charge_block(dt_util.utcnow())
    if not block:
        return {}
    return {"start": block[0].slot.start.isoformat(), "end": block[-1].slot.end.isoformat()}


def _mode_attrs(c: OptimizerCoordinator) -> dict[str, Any]:
    plan = c.data.plan
    cap = c.params.capacity_kwh
    schedule: list[dict[str, Any]] = []
    for step in plan.steps:
        if schedule and schedule[-1]["mode"] == step.mode.value:
            schedule[-1]["end"] = step.slot.end.isoformat()
            schedule[-1]["soc_end"] = round(step.soc_end / cap * 100, 1)
        else:
            schedule.append(
                {
                    "start": step.slot.start.isoformat(),
                    "end": step.slot.end.isoformat(),
                    "mode": step.mode.value,
                    "soc_end": round(step.soc_end / cap * 100, 1),
                }
            )
    slots = plan_rows(plan, cap)
    return {
        "control_active": c.control_enabled,
        "applied_mode": c.applied_mode.value if c.applied_mode else None,
        "planned_at": c.data.created.isoformat(),
        "terminal_price": round(plan.terminal_price, 4),
        "schedule": schedule,
        "slots": slots,
    }


@dataclass(frozen=True, kw_only=True)
class OptimizerSensorDescription(SensorEntityDescription):
    value_fn: Callable[[OptimizerCoordinator], Any]
    attrs_fn: Callable[[OptimizerCoordinator], dict[str, Any]] | None = None


SENSORS: tuple[OptimizerSensorDescription, ...] = (
    OptimizerSensorDescription(
        key="planned_mode",
        device_class=SensorDeviceClass.ENUM,
        options=[m.value for m in Mode],
        value_fn=lambda c: (s.mode.value if (s := c.data.plan.step_at(dt_util.utcnow())) else None),
        attrs_fn=_mode_attrs,
    ),
    OptimizerSensorDescription(
        key="charge_target",
        native_unit_of_measurement=PERCENTAGE,
        value_fn=_charge_target,
        attrs_fn=_charge_target_attrs,
    ),
    OptimizerSensorDescription(
        key="planned_grid_charge",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda c: round(c.data.plan.grid_charge_kwh, 3),
    ),
    OptimizerSensorDescription(
        key="estimated_savings",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda c: round(c.data.plan.savings, 4),
    ),
    OptimizerSensorDescription(
        key="pv_forecast_24h",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=lambda c: round(c.data.pv_next_24h_kwh, 2),
        attrs_fn=lambda c: {
            "source": c.data.pv_source,
            "forecast_integration_kwh": _round(c.data.pv_forecast_24h_kwh),
            "open_meteo_model_kwh": _round(c.data.pv_open_meteo_24h_kwh),
            "correction_factors": c.data.pv_factors,
        },
    ),
    OptimizerSensorDescription(
        key="load_forecast_24h",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=lambda c: round(c.data.load_next_24h_kwh, 2),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OptimizerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(OptimizerSensor(coordinator, d) for d in SENSORS)


class OptimizerSensor(OptimizerEntity, SensorEntity):
    entity_description: OptimizerSensorDescription
    # large / fast changing attributes are not written to the database
    _unrecorded_attributes = frozenset({"schedule", "slots", "correction_factors"})

    def __init__(self, coordinator: OptimizerCoordinator, description: OptimizerSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        if description.device_class == SensorDeviceClass.MONETARY:
            self._attr_native_unit_of_measurement = coordinator.hass.config.currency

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator)
