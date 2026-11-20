"""PV Battery Optimizer: price and forecast driven battery control via evcc."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import OptimizerCoordinator

PLATFORMS = [Platform.SENSOR, Platform.SWITCH]

type OptimizerConfigEntry = ConfigEntry[OptimizerCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: OptimizerConfigEntry) -> bool:
    coordinator = OptimizerCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start_control_loop()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OptimizerConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown_control()
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: OptimizerConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
