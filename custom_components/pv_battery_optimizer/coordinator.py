"""Coordinator: collects data, runs the optimizer and controls evcc."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

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
    CONF_MIN_BENEFIT,
    CONF_MIN_SOC,
    CONF_OPEN_METEO,
    CONF_PASSWORD,
    CONF_PV_ENERGY_ENTITY,
    CONF_PV_FORECAST_ENTRY,
    CONF_PV_SAFETY,
    CONF_PV_SOURCE,
    CONF_SOC_STEP,
    CONF_WEAR_COST,
    CONTROL_INTERVAL,
    DEFAULT_OPTIONS,
    DOMAIN,
    EV_POLICY_BLOCK,
    HISTORY_REFRESH,
    LOGGER,
    MAX_PLAN_AGE,
    PLAN_INTERVAL,
    PV_SAMPLE_RETENTION_DAYS,
    PLAN_RETENTION_DAYS,
    PLAN_SNAPSHOT_HOUR,
    PV_SNAPSHOT_HOUR,
    SOC_LOG_RETENTION_DAYS,
)
from .evaluation import evaluate_plan
from .evcc_api import EvccClient, EvccError, LiveState, Rate, parse_state
from .forecast import (
    PvSample,
    apply_correction,
    combine_pv,
    energy_in_interval,
    forecast_load,
    pv_correction_factors,
)
from .optimizer import BatteryParams, Mode, Plan, Slot, control_mode, optimize, plan_rows
from .open_meteo import OpenMeteoError
from .sources import async_hourly_energy, async_pv_forecast
from .weather_pv import WeatherPvForecast

MAX_HORIZON = timedelta(hours=48)
EV_LIVE_LOOKAHEAD = timedelta(minutes=30)
STORE_VERSION = 1

# Day-ahead forecasts recorded every evening and compared with the actual
# production: (snapshot key, sample key) per forecast. The uncorrected
# forecast integration feeds the correction factors, all three feed the
# comparison in the diagnostics.
DAY_AHEAD = {
    "forecast": ("snapshots", "samples"),
    "forecast_corrected": ("fsc_snapshots", "fsc_samples"),
    "open_meteo": ("om_snapshots", "om_samples"),
}


@dataclass
class OptimizerData:
    plan: Plan
    created: datetime
    soc_kwh: float
    pv_factors: dict[int, float]
    pv_next_24h_kwh: float
    load_next_24h_kwh: float
    pv_source: str
    pv_forecast_24h_kwh: float | None
    pv_open_meteo_24h_kwh: float | None


def _overlap_hours(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds() / 3600)


def _rate_at(rates: list[Rate] | None, when: datetime) -> float | None:
    for r in rates or []:
        if r.start <= when < r.end:
            return r.value
    return None


class OptimizerCoordinator(DataUpdateCoordinator[OptimizerData]):
    """Plans the battery schedule and drives evcc's external battery mode."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=PLAN_INTERVAL,
        )
        self.options: dict[str, Any] = {**DEFAULT_OPTIONS, **entry.options}
        self.client = EvccClient(
            async_get_clientsession(hass),
            entry.data[CONF_EVCC_URL],
            entry.data.get(CONF_PASSWORD),
        )
        self.control_enabled = False
        self.applied_mode: Mode | None = None
        self.live: LiveState | None = None
        self._controlling = False
        self._ev_charging: bool | None = None
        self._load_history: dict[datetime, float] = {}
        self._history_fetched: datetime | None = None
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        # persisted: PV forecast snapshots + learning samples, evening plan
        # snapshots and the measured SoC log (for plan vs. reality)
        self._stored: dict[str, Any] = {}
        self._last_soc_slot: datetime | None = None
        self.weather_pv: WeatherPvForecast | None = None
        if self.options[CONF_OPEN_METEO]:
            self.weather_pv = WeatherPvForecast(
                hass, entry.entry_id, entry.data[CONF_PV_ENERGY_ENTITY]
            )
        self.pv_errors: dict[str, str] = {}

    # ------------------------------------------------------------------ setup

    async def async_setup(self) -> None:
        self._stored = await self._store.async_load() or {}
        self._stored.setdefault("plans", {})
        self._stored.setdefault("soc_log", {})
        for snap_key, sample_key in DAY_AHEAD.values():
            self._stored.setdefault(snap_key, {})
            self._stored.setdefault(sample_key, [])
        if self.weather_pv is not None:
            await self.weather_pv.async_load()

        # The PV correction compares a specific forecast with a specific
        # production sensor. If either changed (reconfigure), start learning
        # from scratch instead of mixing old and new.
        data = self.config_entry.data
        basis = [data[CONF_PV_FORECAST_ENTRY], data[CONF_PV_ENERGY_ENTITY]]
        previous = self._stored.get("pv_learning_basis")
        if previous is not None and previous != basis:
            LOGGER.info("PV forecast or PV sensor changed, resetting learned PV correction")
            for snap_key, sample_key in DAY_AHEAD.values():
                self._stored[snap_key] = {}
                self._stored[sample_key] = []
        if previous != basis:
            self._stored["pv_learning_basis"] = basis
            await self._store.async_save(self._stored)

    @callback
    def async_start_control_loop(self) -> None:
        self.config_entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._async_control_tick, CONTROL_INTERVAL
            )
        )

    async def async_shutdown_control(self) -> None:
        await self._async_release()

    @property
    def params(self) -> BatteryParams:
        o = self.options
        cap = float(o[CONF_CAPACITY])
        return BatteryParams(
            capacity_kwh=cap,
            min_soc_kwh=cap * float(o[CONF_MIN_SOC]) / 100,
            max_soc_kwh=cap * float(o[CONF_MAX_SOC]) / 100,
            max_charge_kw=float(o[CONF_MAX_CHARGE_KW]),
            max_discharge_kw=float(o[CONF_MAX_DISCHARGE_KW]),
            grid_charge_kw=float(o[CONF_GRID_CHARGE_KW]),
            charge_efficiency=float(o[CONF_CHARGE_EFF]),
            discharge_efficiency=float(o[CONF_DISCHARGE_EFF]),
            wear_cost_per_kwh=float(o[CONF_WEAR_COST]),
            soc_step_kwh=float(o[CONF_SOC_STEP]),
            min_benefit=float(o[CONF_MIN_BENEFIT]),
        )

    # --------------------------------------------------------------- planning

    async def _async_update_data(self) -> OptimizerData:
        now = dt_util.utcnow()
        try:
            live = parse_state(await self.client.get_state())
            grid_rates = await self.client.get_tariff("grid")
            feedin_rates = await self.client.get_tariff("feedin")
        except EvccError as err:
            raise UpdateFailed(str(err)) from err
        self.live = live
        self._ev_charging = live.ev_charging

        if live.soc_percent is None:
            raise UpdateFailed("evcc reports no battery SoC")
        if not grid_rates or grid_rates[-1].end <= now:
            raise UpdateFailed("evcc provides no grid tariff for the future")

        params = self.params
        soc_kwh = live.soc_percent / 100 * params.capacity_kwh
        tz = dt_util.get_default_time_zone()

        pv_raw, pv_model = await self._async_pv_forecasts(now)

        await self._async_refresh_load_history(now)

        samples = [
            PvSample(dt_util.parse_datetime(ts), fc, act)
            for ts, fc, act in self._stored["samples"]
        ]
        factors = pv_correction_factors(samples, now, tz)
        pv_corrected = apply_correction(pv_raw, factors, tz) if pv_raw is not None else None
        await self._async_update_pv_learning(
            now,
            {"forecast": pv_raw, "forecast_corrected": pv_corrected, "open_meteo": pv_model},
        )
        pv_selected, pv_source = combine_pv(
            pv_corrected, pv_model, self.options[CONF_PV_SOURCE]
        )
        safety = float(self.options[CONF_PV_SAFETY])
        pv = {k: v * safety for k, v in pv_selected.items()}

        horizon_end = min(grid_rates[-1].end, now + MAX_HORIZON)
        first_hour = now.replace(minute=0, second=0, microsecond=0)
        hours = []
        h = first_hour
        while h < max(horizon_end, now + timedelta(hours=24)):
            hours.append(h)
            h += timedelta(hours=1)
        load = forecast_load(self._load_history, hours, tz)

        slots = self._build_slots(now, horizon_end, grid_rates, feedin_rates, pv, load, live)
        plan = await self.hass.async_add_executor_job(optimize, slots, soc_kwh, params)
        await self._async_snapshot_plan(now, plan, live.soc_percent, horizon_end)

        day_end = now + timedelta(hours=24)
        return OptimizerData(
            plan=plan,
            created=now,
            soc_kwh=soc_kwh,
            pv_factors=factors,
            pv_next_24h_kwh=energy_in_interval(pv, now, day_end),
            load_next_24h_kwh=energy_in_interval(load, now, day_end),
            pv_source=pv_source,
            pv_forecast_24h_kwh=None
            if pv_corrected is None
            else energy_in_interval(pv_corrected, now, day_end),
            pv_open_meteo_24h_kwh=None
            if pv_model is None
            else energy_in_interval(pv_model, now, day_end),
        )

    async def _async_pv_forecasts(
        self, now: datetime
    ) -> tuple[dict[datetime, float] | None, dict[datetime, float] | None]:
        """Forecast integration (uncorrected) and Open-Meteo model forecast.
        Either may be None; planning fails only if both are unavailable."""
        self.pv_errors = {}
        pv_raw: dict[datetime, float] | None = None
        try:
            pv_raw = await async_pv_forecast(
                self.hass, self.config_entry.data[CONF_PV_FORECAST_ENTRY]
            )
        except Exception as err:  # noqa: BLE001 - foreign integration
            self.pv_errors["forecast"] = str(err)
        pv_model: dict[datetime, float] | None = None
        if self.weather_pv is not None:
            try:
                pv_model = await self.weather_pv.async_forecast(now)
            except OpenMeteoError as err:
                self.pv_errors["open_meteo"] = str(err)
        if pv_raw is None and pv_model is None:
            raise UpdateFailed(f"PV forecast unavailable: {self.pv_errors}")
        if self.pv_errors:
            LOGGER.debug("PV forecast problems: %s", self.pv_errors)
        return pv_raw, pv_model

    def _build_slots(
        self,
        now: datetime,
        horizon_end: datetime,
        grid_rates: list[Rate],
        feedin_rates: list[Rate] | None,
        pv: dict[datetime, float],
        load: dict[datetime, float],
        live: LiveState,
    ) -> list[Slot]:
        feedin_default = float(self.options[CONF_FEEDIN_PRICE])
        block_ev = self.options[CONF_EV_POLICY] == EV_POLICY_BLOCK
        slots: list[Slot] = []
        for rate in grid_rates:
            if rate.end <= now or rate.start >= horizon_end:
                continue
            start, end = max(rate.start, now), min(rate.end, horizon_end)
            ev = self._ev_energy(live, now, start, end)
            export = _rate_at(feedin_rates, start)
            slots.append(
                Slot(
                    start=start,
                    end=end,
                    import_price=rate.value,
                    export_price=feedin_default if export is None else export,
                    pv_kwh=energy_in_interval(pv, start, end),
                    load_kwh=energy_in_interval(load, start, end) + ev,
                    ev_kwh=ev,
                    allow_discharge=not (block_ev and ev > 0),
                )
            )
        return slots

    def _ev_energy(
        self, live: LiveState, now: datetime, start: datetime, end: datetime
    ) -> float:
        """Expected EV charging energy in a slot: currently running sessions
        are assumed to continue briefly, evcc charging plans are honoured.
        Unplanned sessions are picked up by re-planning when they start."""
        ev_kw = float(self.options[CONF_EV_CHARGE_KW])
        total = 0.0
        for lp in live.loadpoints:
            running = 0.0
            if lp.charging:
                power = lp.charge_power_w / 1000 or ev_kw
                running = power * _overlap_hours(start, end, now, now + EV_LIVE_LOOKAHEAD)
            planned = sum(
                ev_kw * _overlap_hours(start, end, ps, pe) for ps, pe in lp.plan_slots
            )
            total += max(running, planned)
        return total

    async def _async_refresh_load_history(self, now: datetime) -> None:
        if self._history_fetched and now - self._history_fetched < HISTORY_REFRESH:
            return
        data = self.config_entry.data
        start = now - timedelta(days=int(self.options[CONF_LOAD_HISTORY_DAYS]))
        house = await async_hourly_energy(
            self.hass, data[CONF_HOUSE_ENERGY_ENTITY], start, now
        )
        ev: dict[datetime, float] = {}
        if ev_entity := data.get(CONF_EV_ENERGY_ENTITY):
            ev = await async_hourly_energy(self.hass, ev_entity, start, now)
        self._load_history = {
            ts: max(0.0, kwh - ev.get(ts, 0.0)) for ts, kwh in house.items()
        }
        self._history_fetched = now

    async def _async_update_pv_learning(
        self, now: datetime, forecasts: dict[str, dict[datetime, float] | None]
    ) -> None:
        """Record the day-ahead PV forecasts every evening and, once the day is
        over, compare them with the actual production from the recorder."""
        tz = dt_util.get_default_time_zone()
        local_now = now.astimezone(tz)
        tomorrow = local_now.date() + timedelta(days=1)
        changed = False
        actual_by_day: dict[str, dict[datetime, float]] = {}

        for name, (snap_key, sample_key) in DAY_AHEAD.items():
            snapshots: dict[str, dict[str, float]] = self._stored[snap_key]
            samples: list[list[Any]] = self._stored[sample_key]
            forecast = forecasts.get(name)

            if (
                forecast is not None
                and local_now.hour >= PV_SNAPSHOT_HOUR
                and tomorrow.isoformat() not in snapshots
            ):
                snapshots[tomorrow.isoformat()] = {
                    ts.isoformat(): kwh
                    for ts, kwh in forecast.items()
                    if ts.astimezone(tz).date() == tomorrow
                }
                changed = True

            for day_str in list(snapshots):
                day = date.fromisoformat(day_str)
                day_start = datetime(day.year, day.month, day.day, tzinfo=tz)
                day_end = day_start + timedelta(days=1)
                # wait until hourly statistics of the whole day are compiled
                if now < dt_util.as_utc(day_end) + timedelta(hours=1):
                    continue
                snapshot = snapshots.pop(day_str)
                changed = True
                if day_str not in actual_by_day:
                    actual_by_day[day_str] = await async_hourly_energy(
                        self.hass,
                        self.config_entry.data[CONF_PV_ENERGY_ENTITY],
                        dt_util.as_utc(day_start),
                        dt_util.as_utc(day_end),
                    )
                actual = actual_by_day[day_str]
                for ts_str, fc in snapshot.items():
                    ts = dt_util.parse_datetime(ts_str)
                    if ts is not None and (act := actual.get(ts)) is not None:
                        samples.append([ts_str, fc, act])

            cutoff = now - timedelta(days=PV_SAMPLE_RETENTION_DAYS)
            kept = [s for s in samples if dt_util.parse_datetime(s[0]) >= cutoff]
            if len(kept) != len(samples):
                self._stored[sample_key] = kept
                changed = True
        if changed:
            await self._store.async_save(self._stored)

    # ------------------------------------------------------- plan vs. reality

    async def _async_snapshot_plan(
        self, now: datetime, plan: Plan, soc_percent: float, horizon_end: datetime
    ) -> None:
        """Keep the first plan after PLAN_SNAPSHOT_HOUR that covers all of
        tomorrow - it contains tonight's charging decisions."""
        tz = dt_util.get_default_time_zone()
        local_now = now.astimezone(tz)
        tomorrow = local_now.date() + timedelta(days=1)
        key = tomorrow.isoformat()
        plans: dict[str, Any] = self._stored["plans"]
        if local_now.hour < PLAN_SNAPSHOT_HOUR or key in plans:
            return
        end_of_tomorrow = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=tz) + timedelta(days=1)
        if horizon_end < dt_util.as_utc(end_of_tomorrow):
            return
        plans[key] = {
            "created": now.isoformat(),
            "soc_start": soc_percent,
            "options": dict(self.options),
            "terminal_price": plan.terminal_price,
            "savings": round(plan.savings, 4),
            "rows": plan_rows(plan, self.params.capacity_kwh),
        }
        for old in sorted(plans)[:-PLAN_RETENTION_DAYS]:
            del plans[old]
        await self._store.async_save(self._stored)

    @callback
    def _log_soc(self, now: datetime) -> None:
        """Record the measured SoC once per 15-min slot."""
        if self.live is None or self.live.soc_percent is None:
            return
        slot = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
        if slot == self._last_soc_slot:
            return
        self._last_soc_slot = slot
        log: dict[str, float] = self._stored["soc_log"]
        log[slot.isoformat()] = self.live.soc_percent
        cutoff = (now - timedelta(days=SOC_LOG_RETENTION_DAYS)).isoformat()
        for ts in [ts for ts in log if ts < cutoff]:
            del log[ts]
        self._store.async_delay_save(lambda: self._stored, 300)

    async def async_evaluate_plans(self) -> dict[str, Any]:
        """Plan vs. reality for all saved evening plans."""
        now = dt_util.utcnow()
        data = self.config_entry.data
        soc_log = {dt_util.parse_datetime(ts): soc for ts, soc in self._stored["soc_log"].items()}
        result: dict[str, Any] = {}
        for day, snapshot in sorted(self._stored["plans"].items()):
            rows = snapshot["rows"]
            if not rows:
                continue
            start = dt_util.parse_datetime(rows[0]["start"]).replace(minute=0, second=0, microsecond=0)
            end = min(now, dt_util.parse_datetime(rows[-1]["start"]) + timedelta(hours=1))
            if end <= start:
                continue
            pv = await async_hourly_energy(self.hass, data[CONF_PV_ENERGY_ENTITY], start, end)
            house = await async_hourly_energy(self.hass, data[CONF_HOUSE_ENERGY_ENTITY], start, end)
            ev: dict[datetime, float] = {}
            if ev_entity := data.get(CONF_EV_ENERGY_ENTITY):
                ev = await async_hourly_energy(self.hass, ev_entity, start, end)
            house_only = {ts: max(0.0, kwh - ev.get(ts, 0.0)) for ts, kwh in house.items()}
            result[day] = {
                "created": snapshot["created"],
                "soc_start": snapshot["soc_start"],
                "planned_savings": snapshot["savings"],
                **evaluate_plan(rows, pv, house_only, ev, soc_log, now),
            }
        return result

    @property
    def stored(self) -> dict[str, Any]:
        return self._stored

    # ---------------------------------------------------------------- control

    def desired_mode(self) -> Mode | None:
        """Mode the battery should be in right now, None = let evcc decide."""
        if self.data is None or self.live is None or self.live.soc_percent is None:
            return None
        now = dt_util.utcnow()
        if now - self.data.created > MAX_PLAN_AGE:
            return None
        soc_kwh = self.live.soc_percent / 100 * self.params.capacity_kwh
        return control_mode(self.data.plan, now, soc_kwh)

    async def _async_control_tick(self, _now: datetime | None = None) -> None:
        try:
            self.live = parse_state(await self.client.get_state())
        except EvccError as err:
            LOGGER.debug("Control tick: %s", err)
            return
        self._log_soc(dt_util.utcnow())
        if self._ev_charging is not None and self.live.ev_charging != self._ev_charging:
            LOGGER.debug("EV charging state changed, re-planning")
            self._ev_charging = self.live.ev_charging
            await self.async_request_refresh()
        await self._async_apply()

    async def _async_apply(self) -> None:
        mode = self.desired_mode() if self.control_enabled else None
        if mode is None:
            await self._async_release()
            return
        try:
            await self.client.set_battery_mode(mode.value)
        except EvccError as err:
            LOGGER.warning("Setting evcc battery mode failed: %s", err)
            return
        self._controlling = True
        if mode != self.applied_mode:
            LOGGER.info("Battery mode -> %s", mode.value)
            self.applied_mode = mode
            self.async_update_listeners()

    async def _async_release(self) -> None:
        if not self._controlling:
            return
        try:
            await self.client.clear_battery_mode()
        except EvccError as err:
            # evcc drops the external mode after 60 s anyway
            LOGGER.debug("Releasing evcc battery mode failed: %s", err)
        self._controlling = False
        self.applied_mode = None
        self.async_update_listeners()

    async def async_set_control(self, enabled: bool) -> None:
        self.control_enabled = enabled
        await self._async_apply()
