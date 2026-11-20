"""Switch enabling active control of the battery via evcc."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import OptimizerConfigEntry
from .entity import OptimizerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OptimizerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ActiveControlSwitch(entry.runtime_data, "active_control")])


class ActiveControlSwitch(OptimizerEntity, SwitchEntity, RestoreEntity):
    """Off = dry run: the plan is computed and published, evcc stays in charge."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_ON:
            await self.coordinator.async_set_control(True)

    @property
    def is_on(self) -> bool:
        return self.coordinator.control_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_control(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_control(False)
        self.async_write_ha_state()
