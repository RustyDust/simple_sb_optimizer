"""Home Assistant level tests: config flow, setup, sensors, control loop.

Run: .venv/bin/pytest tests
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util

from custom_components.pv_battery_optimizer.const import (
    CONF_EV_ENERGY_ENTITY,
    CONF_EVCC_URL,
    CONF_HOUSE_ENERGY_ENTITY,
    CONF_PV_ENERGY_ENTITY,
    CONF_PV_FORECAST_ENTRY,
    DEFAULT_OPTIONS,
    DOMAIN,
)

URL = "http://evcc.test:7070"
PKG = "custom_components.pv_battery_optimizer"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def evcc_state(soc: float = 30.0, charging: bool = False) -> dict:
    return {
        "battery": {"soc": soc, "capacity": 20.0, "power": 0},
        "batteryMode": "normal",
        "loadpoints": [{"mode": "pv", "charging": charging, "chargePower": 11000 if charging else 0}],
    }


def grid_rates(now: datetime) -> dict:
    """24 h of 15-min prices: cheap night (first 6 h), expensive evening."""
    start = now.replace(minute=0, second=0, microsecond=0)
    rates = []
    for i in range(96):
        s = start + timedelta(minutes=15 * i)
        price = 0.10 if i < 24 else (0.45 if i < 40 else 0.25)
        rates.append({"start": _iso(s), "end": _iso(s + timedelta(minutes=15)), "value": price})
    return {"rates": rates}


def mock_evcc(aioclient_mock, soc: float = 30.0) -> None:
    aioclient_mock.get(f"{URL}/api/state", json=evcc_state(soc))
    aioclient_mock.get(f"{URL}/api/tariff/grid", json=grid_rates(dt_util.utcnow()))
    aioclient_mock.get(f"{URL}/api/tariff/feedin", status=404)


async def test_config_flow(hass: HomeAssistant, aioclient_mock) -> None:
    MockConfigEntry(domain="forecast_solar", title="Roof").add_to_hass(hass)
    aioclient_mock.get(f"{URL}/api/state", json=evcc_state())

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EVCC_URL: URL + "/"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sources"

    fs_entry = hass.config_entries.async_entries("forecast_solar")[0]
    with patch(f"{PKG}.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_PV_FORECAST_ENTRY: fs_entry.entry_id,
                CONF_PV_ENERGY_ENTITY: "sensor.pv_energy",
                CONF_HOUSE_ENERGY_ENTITY: "sensor.house_energy",
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_EVCC_URL] == URL
    assert result["options"]["capacity_kwh"] == 20.0


async def test_config_flow_cannot_connect(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/api/state", status=500)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_EVCC_URL: URL})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_EVCC_URL: URL}, options=DEFAULT_OPTIONS)
    entry.add_to_hass(hass)
    with patch(f"{PKG}.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        new = {**DEFAULT_OPTIONS, "grid_charge_kw": 4.6}
        result = await hass.config_entries.options.async_configure(result["flow_id"], new)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["grid_charge_kw"] == 4.6


@pytest.fixture
async def setup_entry(hass: HomeAssistant, aioclient_mock):
    """Set up the integration with mocked evcc, PV forecast and history."""
    mock_evcc(aioclient_mock)
    aioclient_mock.post(f"{URL}/api/batterymode/normal", json=1)
    aioclient_mock.post(f"{URL}/api/batterymode/hold", json=2)
    aioclient_mock.post(f"{URL}/api/batterymode/charge", json=3)
    aioclient_mock.delete(f"{URL}/api/batterymode", json=0)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EVCC_URL: URL,
            CONF_PV_FORECAST_ENTRY: "fs",
            CONF_PV_ENERGY_ENTITY: "sensor.pv_energy",
            CONF_HOUSE_ENERGY_ENTITY: "sensor.house_energy",
            CONF_EV_ENERGY_ENTITY: "sensor.ev_energy",
        },
        options=DEFAULT_OPTIONS,
    )
    entry.add_to_hass(hass)

    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    history = {now - timedelta(hours=h): 1.0 for h in range(1, 24 * 14)}  # 1 kW flat

    async def fake_history(hass, statistic_id, start, end):
        return {} if statistic_id == "sensor.ev_energy" else history

    with (
        patch(f"{PKG}.coordinator.async_pv_forecast", return_value={}),
        patch(f"{PKG}.coordinator.async_hourly_energy", side_effect=fake_history),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry


async def test_setup_creates_plan_and_sensors(hass: HomeAssistant, setup_entry) -> None:
    mode = hass.states.get("sensor.pv_battery_optimizer_planned_battery_mode")
    assert mode is not None
    # cheap now, expensive later, no PV: hold (don't drain the battery in cheap
    # hours), then charge in the cheap window, then use it
    assert mode.state == "hold"
    assert mode.attributes["control_active"] is False
    modes = [b["mode"] for b in mode.attributes["schedule"]]
    assert modes == ["hold", "charge", "normal"]

    grid_charge = hass.states.get("sensor.pv_battery_optimizer_planned_grid_charge")
    assert float(grid_charge.state) > 0

    target = hass.states.get("sensor.pv_battery_optimizer_charge_target")
    assert 30 < float(target.state) <= 100

    switch = hass.states.get("switch.pv_battery_optimizer_active_control")
    assert switch.state == "off"


async def test_dry_run_sends_nothing(hass: HomeAssistant, setup_entry, aioclient_mock) -> None:
    coordinator = setup_entry.runtime_data
    await coordinator._async_control_tick()
    assert not [c for c in aioclient_mock.mock_calls if c[0] in ("POST", "DELETE")]


async def test_active_control_sends_mode_and_releases(
    hass: HomeAssistant, setup_entry, aioclient_mock
) -> None:
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.pv_battery_optimizer_active_control"}, blocking=True
    )
    planned = hass.states.get("sensor.pv_battery_optimizer_planned_battery_mode").state
    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert posts and str(posts[-1][1]).endswith(f"/api/batterymode/{planned}")
    assert hass.states.get("switch.pv_battery_optimizer_active_control").state == "on"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.pv_battery_optimizer_active_control"}, blocking=True
    )
    assert any(c[0] == "DELETE" for c in aioclient_mock.mock_calls)


async def test_unload_releases_control(hass: HomeAssistant, setup_entry, aioclient_mock) -> None:
    await setup_entry.runtime_data.async_set_control(True)
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert any(c[0] == "DELETE" for c in aioclient_mock.mock_calls)

