"""Config flow for PV Battery Optimizer."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_CAPACITY,
    CONF_CHARGE_EFF,
    CONF_DISCHARGE_EFF,
    CONF_EV_CHARGE_KW,
    CONF_EV_ENERGY_ENTITY,
    CONF_EV_POLICY,
    CONF_EVCC_URL,
    CONF_FEEDIN_PRICE,
    CONF_GRID_CHARGE_KW,
    CONF_HOUSE_ENERGY_ENTITY,
    CONF_LOAD_HISTORY_DAYS,
    CONF_MAX_CHARGE_KW,
    CONF_MAX_DISCHARGE_KW,
    CONF_MAX_SOC,
    CONF_MIN_SOC,
    CONF_PASSWORD,
    CONF_PV_ENERGY_ENTITY,
    CONF_PV_FORECAST_ENTRY,
    CONF_PV_SAFETY,
    CONF_SOC_STEP,
    CONF_WEAR_COST,
    DEFAULT_OPTIONS,
    DOMAIN,
    EV_POLICY_BLOCK,
    EV_POLICY_OPTIMIZE,
    PV_FORECAST_DOMAINS,
)
from .evcc_api import EvccAuthError, EvccClient, EvccError, parse_state

ENERGY_SENSOR = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="energy"))


def _num(min_: float, max_: float, step: float, unit: str | None = None) -> NumberSelector:
    config = NumberSelectorConfig(min=min_, max=max_, step=step, mode=NumberSelectorMode.BOX)
    if unit is not None:  # the selector schema rejects None
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CAPACITY): _num(1, 200, 0.1, "kWh"),
        vol.Required(CONF_MIN_SOC): _num(0, 100, 1, "%"),
        vol.Required(CONF_MAX_SOC): _num(10, 100, 1, "%"),
        vol.Required(CONF_MAX_CHARGE_KW): _num(0.1, 50, 0.1, "kW"),
        vol.Required(CONF_MAX_DISCHARGE_KW): _num(0.1, 50, 0.1, "kW"),
        vol.Required(CONF_GRID_CHARGE_KW): _num(0.1, 50, 0.1, "kW"),
        vol.Required(CONF_CHARGE_EFF): _num(0.5, 1, 0.01),
        vol.Required(CONF_DISCHARGE_EFF): _num(0.5, 1, 0.01),
        vol.Required(CONF_WEAR_COST): _num(0, 1, 0.001, "/kWh"),
        vol.Required(CONF_FEEDIN_PRICE): _num(0, 1, 0.001, "/kWh"),
        vol.Required(CONF_EV_CHARGE_KW): _num(1, 50, 0.1, "kW"),
        vol.Required(CONF_EV_POLICY): SelectSelector(
            SelectSelectorConfig(
                options=[EV_POLICY_OPTIMIZE, EV_POLICY_BLOCK],
                translation_key=CONF_EV_POLICY,
            )
        ),
        vol.Required(CONF_PV_SAFETY): _num(0.5, 1.2, 0.01),
        vol.Required(CONF_LOAD_HISTORY_DAYS): _num(7, 365, 1, "d"),
        vol.Required(CONF_SOC_STEP): _num(0.02, 0.5, 0.01, "kWh"),
    }
)


class PvBatteryOptimizerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._capacity: float | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_EVCC_URL].rstrip("/")
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()
            client = EvccClient(
                async_get_clientsession(self.hass), url, user_input.get(CONF_PASSWORD)
            )
            try:
                if user_input.get(CONF_PASSWORD):
                    await client.login()
                live = parse_state(await client.get_state())
            except EvccAuthError:
                errors["base"] = "invalid_auth"
            except EvccError:
                errors["base"] = "cannot_connect"
            else:
                if live.soc_percent is None:
                    errors["base"] = "no_battery"
                else:
                    self._data = {**user_input, CONF_EVCC_URL: url}
                    self._capacity = live.capacity_kwh
                    return await self.async_step_sources()

        schema = vol.Schema(
            {
                vol.Required(CONF_EVCC_URL, default="http://evcc.local:7070"): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.URL)
                ),
                vol.Optional(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_sources(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        forecast_entries = [
            e for e in self.hass.config_entries.async_entries() if e.domain in PV_FORECAST_DOMAINS
        ]
        if not forecast_entries:
            return self.async_abort(reason="no_pv_forecast")

        if user_input is not None:
            options = dict(DEFAULT_OPTIONS)
            if self._capacity:
                options[CONF_CAPACITY] = self._capacity
            return self.async_create_entry(
                title="PV Battery Optimizer",
                data={**self._data, **user_input},
                options=options,
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_PV_FORECAST_ENTRY): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=e.entry_id, label=f"{e.title} ({e.domain})")
                            for e in forecast_entries
                        ]
                    )
                ),
                vol.Required(CONF_PV_ENERGY_ENTITY): ENERGY_SENSOR,
                vol.Required(CONF_HOUSE_ENERGY_ENTITY): ENERGY_SENSOR,
                vol.Optional(CONF_EV_ENERGY_ENTITY): ENERGY_SENSOR,
            }
        )
        return self.async_show_form(step_id="sources", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PvBatteryOptimizerOptionsFlow()


class PvBatteryOptimizerOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, current),
        )
