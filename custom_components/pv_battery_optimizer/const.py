"""Constants for the PV Battery Optimizer integration."""

from __future__ import annotations

from datetime import timedelta
import logging

DOMAIN = "pv_battery_optimizer"
LOGGER = logging.getLogger(__package__)

# entry.data
CONF_EVCC_URL = "evcc_url"
CONF_PASSWORD = "password"
CONF_PV_FORECAST_ENTRY = "pv_forecast_entry"
CONF_PV_ENERGY_ENTITY = "pv_energy_entity"
CONF_HOUSE_ENERGY_ENTITY = "house_energy_entity"
CONF_EV_ENERGY_ENTITY = "ev_energy_entity"

# entry.options
CONF_CAPACITY = "capacity_kwh"
CONF_MIN_SOC = "min_soc"
CONF_MAX_SOC = "max_soc"
CONF_MAX_CHARGE_KW = "max_charge_kw"
CONF_MAX_DISCHARGE_KW = "max_discharge_kw"
CONF_GRID_CHARGE_KW = "grid_charge_kw"
CONF_CHARGE_EFF = "charge_efficiency"
CONF_DISCHARGE_EFF = "discharge_efficiency"
CONF_WEAR_COST = "wear_cost"
CONF_FEEDIN_PRICE = "feedin_price"
CONF_EV_CHARGE_KW = "ev_charge_kw"
CONF_EV_POLICY = "ev_discharge_policy"
CONF_PV_SAFETY = "pv_safety_factor"
CONF_LOAD_HISTORY_DAYS = "load_history_days"
CONF_SOC_STEP = "soc_step_kwh"
CONF_MIN_BENEFIT = "min_benefit"
CONF_OPEN_METEO = "open_meteo"
CONF_PV_SOURCE = "pv_source"

EV_POLICY_OPTIMIZE = "optimize"
EV_POLICY_BLOCK = "block"

# PV forecast used for planning
PV_SOURCE_FORECAST = "forecast"  # forecast integration with learned correction
PV_SOURCE_OPEN_METEO = "open_meteo"  # own model on Open-Meteo irradiance
PV_SOURCE_AVERAGE = "average"

DEFAULT_OPTIONS = {
    CONF_CAPACITY: 20.0,
    CONF_MIN_SOC: 10.0,
    CONF_MAX_SOC: 100.0,
    CONF_MAX_CHARGE_KW: 3.3,
    CONF_MAX_DISCHARGE_KW: 3.3,
    CONF_GRID_CHARGE_KW: 3.3,
    CONF_CHARGE_EFF: 0.95,
    CONF_DISCHARGE_EFF: 0.95,
    CONF_WEAR_COST: 0.03,
    CONF_FEEDIN_PRICE: 0.08,
    CONF_EV_CHARGE_KW: 11.0,
    CONF_EV_POLICY: EV_POLICY_OPTIMIZE,
    CONF_PV_SAFETY: 1.0,
    CONF_LOAD_HISTORY_DAYS: 42,
    CONF_SOC_STEP: 0.05,
    CONF_MIN_BENEFIT: 0.01,
    CONF_OPEN_METEO: False,
    CONF_PV_SOURCE: PV_SOURCE_FORECAST,
}

# Integrations providing an "energy" platform with async_get_solar_forecast
PV_FORECAST_DOMAINS = ("forecast_solar", "solcast_solar", "open_meteo_solar_forecast")

PLAN_INTERVAL = timedelta(minutes=5)
CONTROL_INTERVAL = timedelta(seconds=20)  # evcc resets external mode after 60 s
HISTORY_REFRESH = timedelta(hours=1)
MAX_PLAN_AGE = timedelta(minutes=30)
PV_SNAPSHOT_HOUR = 18  # local hour after which tomorrow's forecast is recorded
PV_SAMPLE_RETENTION_DAYS = 400
PLAN_SNAPSHOT_HOUR = 21  # local hour after which the plan for tomorrow is saved
PLAN_RETENTION_DAYS = 30
SOC_LOG_RETENTION_DAYS = 31
