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


def grid_rates(now: datetime, hours: int = 24) -> dict:
    """15-min prices: cheap first 6 h, expensive 4 h, then medium."""
    start = now.replace(minute=0, second=0, microsecond=0)
    rates = []
    for i in range(hours * 4):
        s = start + timedelta(minutes=15 * i)
        price = 0.10 if i < 24 else (0.45 if i < 40 else 0.25)
        rates.append({"start": _iso(s), "end": _iso(s + timedelta(minutes=15)), "value": price})
    return {"rates": rates}


def mock_evcc(aioclient_mock, soc: float = 30.0, hours: int = 24) -> None:
    aioclient_mock.get(f"{URL}/api/state", json=evcc_state(soc))
    aioclient_mock.get(f"{URL}/api/tariff/grid", json=grid_rates(dt_util.utcnow(), hours))
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
def rate_hours() -> int:
    """Hours of grid prices evcc returns (override via parametrize)."""
    return 24


@pytest.fixture
def start_time() -> datetime | None:
    """Frozen start time (override via parametrize), None = real time."""
    return None


@pytest.fixture
def stored_data() -> dict | None:
    """Pre-existing persisted integration data (override via parametrize)."""
    return None


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant, aioclient_mock, freezer, hass_storage, rate_hours, start_time, stored_data
):
    """Set up the integration with mocked evcc, PV forecast and history."""
    if start_time is not None:
        freezer.move_to(start_time)
    if stored_data is not None:
        hass_storage[f"{DOMAIN}.test_entry"] = {
            "version": 1, "minor_version": 1, "key": f"{DOMAIN}.test_entry", "data": stored_data
        }
    mock_evcc(aioclient_mock, hours=rate_hours)
    aioclient_mock.post(f"{URL}/api/batterymode/normal", json=1)
    aioclient_mock.post(f"{URL}/api/batterymode/hold", json=2)
    aioclient_mock.post(f"{URL}/api/batterymode/charge", json=3)
    aioclient_mock.delete(f"{URL}/api/batterymode", json=0)

    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="test_entry",
        unique_id=URL,
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

    async def fake_history(hass, statistic_id, start, end):
        """1 kWh per hour for house and PV, no EV."""
        if statistic_id == "sensor.ev_energy":
            return {}
        hour = start.replace(minute=0, second=0, microsecond=0)
        out = {}
        while hour < end:
            out[hour] = 1.0
            hour += timedelta(hours=1)
        return out

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



@pytest.mark.parametrize(
    ("rate_hours", "start_time"),
    # 05:00 UTC = 21:00 in the test time zone (US/Pacific, PST)
    [(48, datetime(2026, 11, 20, 5, 30, tzinfo=dt_util.UTC))],
)
async def test_evening_plan_snapshot_and_diagnostics(
    hass: HomeAssistant, setup_entry, freezer
) -> None:
    from custom_components.pv_battery_optimizer.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    coordinator = setup_entry.runtime_data
    assert dt_util.now().hour == 21
    plans = coordinator.stored["plans"]
    assert list(plans) == ["2026-11-20"]  # tomorrow in local time
    assert plans["2026-11-20"]["rows"][0]["mode"] in ("hold", "charge", "normal")

    # log SoC during the next hours, then evaluate
    for minutes in range(0, 3 * 60, 15):
        freezer.move_to(datetime(2026, 11, 20, 5, 45, tzinfo=dt_util.UTC) + timedelta(minutes=minutes))
        await coordinator._async_control_tick()
    freezer.move_to(datetime(2026, 11, 20, 9, 0, tzinfo=dt_util.UTC))

    diag = await async_get_config_entry_diagnostics(hass, setup_entry)
    assert "password" not in diag["entry"]["data"]
    assert diag["current_plan"]["rows"]
    day = diag["plan_vs_reality"]["2026-11-20"]
    summary = day["summary"]
    assert summary["hours_evaluated"] >= 2
    assert summary["pv_actual"] > 0
    assert "soc_error_mean" in summary
    assert day["hours"][0]["partial"] is True


async def test_no_snapshot_before_evening(hass: HomeAssistant, setup_entry) -> None:
    coordinator = setup_entry.runtime_data
    if dt_util.now().hour < 21:
        assert coordinator.stored["plans"] == {}


def _reconfigure_entry(hass: HomeAssistant) -> tuple[MockConfigEntry, MockConfigEntry]:
    fs = MockConfigEntry(domain="forecast_solar", title="Roof")
    fs.add_to_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=URL,
        data={
            CONF_EVCC_URL: URL,
            "password": "secret",
            CONF_PV_FORECAST_ENTRY: fs.entry_id,
            CONF_PV_ENERGY_ENTITY: "sensor.pv_energy",
            CONF_HOUSE_ENERGY_ENTITY: "sensor.wrong_house",
            CONF_EV_ENERGY_ENTITY: "sensor.ev_energy",
        },
        options={**DEFAULT_OPTIONS, "grid_charge_kw": 4.6},
    )
    entry.add_to_hass(hass)
    return entry, fs


async def test_reconfigure_changes_sources_keeps_password_and_options(
    hass: HomeAssistant, aioclient_mock
) -> None:
    entry, fs = _reconfigure_entry(hass)
    aioclient_mock.post(f"{URL}/api/auth/login", json=True, cookies={"auth": "token"})
    aioclient_mock.get(f"{URL}/api/state", json=evcc_state())

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    with patch(f"{PKG}.async_setup_entry", return_value=True) as setup:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_EVCC_URL: URL,
                CONF_PV_FORECAST_ENTRY: fs.entry_id,
                CONF_PV_ENERGY_ENTITY: "sensor.pv_energy",
                CONF_HOUSE_ENERGY_ENTITY: "sensor.house_energy",
                # EV sensor cleared
            },
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOUSE_ENERGY_ENTITY] == "sensor.house_energy"
    assert CONF_EV_ENERGY_ENTITY not in entry.data
    assert entry.data["password"] == "secret"
    assert entry.options["grid_charge_kw"] == 4.6
    assert setup.called  # reloaded


async def test_reconfigure_cannot_connect(hass: HomeAssistant, aioclient_mock) -> None:
    entry, fs = _reconfigure_entry(hass)
    aioclient_mock.post(f"{URL}/api/auth/login", status=500)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EVCC_URL: URL,
            CONF_PV_FORECAST_ENTRY: fs.entry_id,
            CONF_PV_ENERGY_ENTITY: "sensor.pv_energy",
            CONF_HOUSE_ENERGY_ENTITY: "sensor.house_energy",
        },
    )
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data[CONF_HOUSE_ENERGY_ENTITY] == "sensor.wrong_house"


SAMPLE = ["2026-11-19T11:00:00+00:00", 1.0, 0.8]


@pytest.mark.parametrize(
    "stored_data",
    [{"samples": [SAMPLE], "pv_learning_basis": ["fs", "sensor.old_pv"]}],
)
async def test_pv_learning_reset_when_pv_sensor_changed(hass: HomeAssistant, setup_entry) -> None:
    stored = setup_entry.runtime_data.stored
    assert stored["samples"] == []
    assert stored["pv_learning_basis"] == ["fs", "sensor.pv_energy"]


@pytest.mark.parametrize(
    "stored_data",
    [{"samples": [SAMPLE], "pv_learning_basis": ["fs", "sensor.pv_energy"]}],
)
async def test_pv_learning_kept_when_basis_unchanged(hass: HomeAssistant, setup_entry) -> None:
    assert setup_entry.runtime_data.stored["samples"] == [SAMPLE]


@pytest.mark.parametrize("stored_data", [{"samples": [SAMPLE]}])
async def test_pv_learning_kept_for_existing_installs(hass: HomeAssistant, setup_entry) -> None:
    """Data saved before the basis was tracked is kept and gets a basis."""
    stored = setup_entry.runtime_data.stored
    assert stored["samples"] == [SAMPLE]
    assert stored["pv_learning_basis"] == ["fs", "sensor.pv_energy"]
