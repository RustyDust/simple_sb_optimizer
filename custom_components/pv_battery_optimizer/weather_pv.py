"""Open-Meteo based PV forecast: keeps the weather history, trains the model
on the recorder's production statistics and predicts the coming days."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER
from .open_meteo import OpenMeteoClient, OpenMeteoError
from .pvmodel import PvModel, WeatherHour
from .sources import async_hourly_energy

HISTORY_DAYS = 730
BACKTEST_DAYS = 30
TRAINING_REFRESH = timedelta(days=1)
FORECAST_REFRESH = timedelta(hours=1)
RETRY = timedelta(hours=1)
# keep using an older weather forecast for a while if Open-Meteo is down
FORECAST_MAX_AGE = timedelta(hours=6)
STORE_VERSION = 1


class WeatherPvForecast:
    """PV forecast from Open-Meteo irradiance and a model of this system."""

    def __init__(self, hass: HomeAssistant, entry_id: str, pv_entity: str) -> None:
        self.hass = hass
        self.pv_entity = pv_entity
        # ~1 km precision is plenty for weather models with 2+ km grids
        self.location = [round(hass.config.latitude, 2), round(hass.config.longitude, 2)]
        self.client = OpenMeteoClient(async_get_clientsession(hass), *self.location)
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.weather"
        )
        self._weather: dict[datetime, WeatherHour] = {}
        self._fetched_until: date | None = None
        self._model: PvModel | None = None
        self._trained: datetime | None = None
        self._next_training: datetime | None = None
        self._forecast_weather: dict[datetime, WeatherHour] = {}
        self._forecast_fetched: datetime | None = None
        self._prediction: dict[datetime, float] | None = None
        self.backtest: dict[str, Any] | None = None
        self.last_error: str | None = None

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        if data.get("location") != self.location:
            return  # nothing cached yet, or HA's home location changed
        self._weather = {
            dt_util.utc_from_timestamp(int(ts)): WeatherHour(*values)
            for ts, values in data.get("hours", {}).items()
        }
        if until := data.get("fetched_until"):
            self._fetched_until = date.fromisoformat(until)

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "location": self.location,
                "fetched_until": self._fetched_until.isoformat() if self._fetched_until else None,
                "hours": {
                    str(int(ts.timestamp())): [w.ghi, w.dni, w.dhi]
                    for ts, w in sorted(self._weather.items())
                },
            }
        )

    async def async_forecast(self, now: datetime) -> dict[datetime, float]:
        """Hourly PV forecast (kWh, keyed by UTC hour start). Hours the model
        can't judge yet (no similar sun position in the history) are missing.

        Raises OpenMeteoError if no forecast is available."""
        if self._next_training is None or now >= self._next_training:
            try:
                await self._async_train(now)
                self._next_training = now + TRAINING_REFRESH
            except OpenMeteoError as err:
                self._next_training = now + RETRY
                self.last_error = f"training: {err}"
                LOGGER.warning("Open-Meteo PV model training failed: %s", err)
                if self._model is None:
                    raise
        if self._model is None or self._model.size == 0:
            raise OpenMeteoError("no production history overlapping the weather history")

        if self._forecast_fetched is None or now - self._forecast_fetched >= FORECAST_REFRESH:
            try:
                self._forecast_weather = await self.client.async_forecast()
                self._forecast_fetched = now
                self._prediction = None
                self.last_error = None
            except OpenMeteoError as err:
                self.last_error = f"forecast: {err}"
                if not self._forecast_weather or now - self._forecast_fetched > FORECAST_MAX_AGE:
                    raise
                LOGGER.debug("Open-Meteo forecast failed, using older one: %s", err)

        if self._prediction is None:
            self._prediction = await self.hass.async_add_executor_job(
                self._model.predict, dict(self._forecast_weather)
            )
        return self._prediction

    async def _async_train(self, now: datetime) -> None:
        end = now.date() - timedelta(days=1)
        if self._fetched_until is None:
            start = now.date() - timedelta(days=HISTORY_DAYS)
        else:
            # re-fetch the last days, the archive fills in with a delay
            start = self._fetched_until - timedelta(days=2)
        if start <= end:
            LOGGER.debug("Fetching Open-Meteo weather history %s - %s", start, end)
            history = await self.client.async_history(start, end)
            for ts, w in history.items():
                if w.ghi > 0:
                    self._weather[ts] = w
                else:
                    self._weather.pop(ts, None)
            self._fetched_until = end
            cutoff = now - timedelta(days=HISTORY_DAYS)
            for ts in [ts for ts in self._weather if ts < cutoff]:
                del self._weather[ts]
            await self._async_save()

        actual = await async_hourly_energy(
            self.hass, self.pv_entity, now - timedelta(days=HISTORY_DAYS), now
        )
        lat, lon = self.location
        weather = dict(self._weather)
        tz = dt_util.get_default_time_zone()
        since = now - timedelta(days=BACKTEST_DAYS)

        def build() -> tuple[PvModel, dict[str, Any]]:
            model = PvModel.build(lat, lon, weather, actual)
            return model, model.backtest(since, tz)

        self._model, self.backtest = await self.hass.async_add_executor_job(build)
        self._trained = now
        self._prediction = None
        LOGGER.debug(
            "Open-Meteo PV model trained on %s hours, backtest %s", self._model.size, self.backtest
        )

    def diagnostics(self) -> dict[str, Any]:
        """Status without the location."""
        return {
            "trained": self._trained.isoformat() if self._trained else None,
            "training_hours": self._model.size if self._model else 0,
            "weather_history_from": min(self._weather).isoformat() if self._weather else None,
            "weather_history_until": self._fetched_until.isoformat() if self._fetched_until else None,
            "max_hourly_kwh": round(self._model.cap_kwh, 3) if self._model else None,
            "forecast_fetched": self._forecast_fetched.isoformat() if self._forecast_fetched else None,
            "last_error": self.last_error,
            f"backtest_last_{BACKTEST_DAYS}_days": self.backtest,
        }
