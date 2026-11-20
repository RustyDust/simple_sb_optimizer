"""Data sources inside Home Assistant: recorder statistics and PV forecast."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .forecast import to_hourly


async def async_hourly_energy(
    hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime
) -> dict[datetime, float]:
    """Hourly energy (kWh) of a total/total_increasing energy sensor from the
    recorder's long term statistics. Keys are UTC hour starts."""
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "hour",
        {"energy": UnitOfEnergy.KILO_WATT_HOUR},
        {"change"},
    )
    result: dict[datetime, float] = {}
    for row in stats.get(statistic_id, []):
        change = row.get("change")
        if change is None:
            continue
        row_start = row["start"]
        if isinstance(row_start, (int, float)):
            row_start = dt_util.utc_from_timestamp(row_start)
        result[dt_util.as_utc(row_start)] = float(change)
    return result


async def async_pv_forecast(hass: HomeAssistant, entry_id: str) -> dict[datetime, float]:
    """Hourly PV forecast (kWh, keyed by UTC hour start) from any integration
    implementing the energy dashboard's solar forecast platform
    (Forecast.Solar, Solcast, Open-Meteo Solar Forecast, ...)."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        raise ValueError(f"PV forecast config entry {entry_id} not found")
    integration = await async_get_integration(hass, entry.domain)
    platform = await integration.async_get_platform("energy")
    data = await platform.async_get_solar_forecast(hass, entry_id)
    if not data:
        return {}
    points: dict[datetime, float] = {}
    for ts, wh in data.get("wh_hours", {}).items():
        dt = dt_util.parse_datetime(ts)
        if dt is not None and wh is not None:
            points[dt_util.as_utc(dt)] = float(wh) / 1000
    return to_hourly(points)
