"""Open-Meteo client: archived and current weather forecasts (irradiance).

No API key needed. Only latitude/longitude (rounded) are sent.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

import aiohttp

from .pvmodel import WeatherHour

HISTORY_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
VARIABLES = "shortwave_radiation,direct_normal_irradiance,diffuse_radiation"
TIMEOUT = aiohttp.ClientTimeout(total=60)


class OpenMeteoError(Exception):
    """Open-Meteo request failed."""


def parse_hourly(payload: dict[str, Any]) -> dict[datetime, WeatherHour]:
    """Hourly irradiance keyed by UTC hour start.

    Open-Meteo's hourly radiation values are means over the *preceding*
    hour, so the value stamped 10:00 belongs to 09:00-10:00."""
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    ghi = hourly.get("shortwave_radiation") or []
    dni = hourly.get("direct_normal_irradiance") or []
    dhi = hourly.get("diffuse_radiation") or []
    out: dict[datetime, WeatherHour] = {}
    for i, ts in enumerate(times):
        try:
            g, n, d = ghi[i], dni[i], dhi[i]
        except IndexError:
            break
        if g is None or n is None or d is None:
            continue
        start = datetime.fromtimestamp(int(ts), UTC) - timedelta(hours=1)
        out[start] = WeatherHour(ghi=float(g), dni=float(n), dhi=float(d))
    return out


class OpenMeteoClient:
    def __init__(self, session: aiohttp.ClientSession, lat: float, lon: float) -> None:
        self._session = session
        self.lat = lat
        self.lon = lon

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        query = {
            "latitude": self.lat,
            "longitude": self.lon,
            "hourly": VARIABLES,
            "timeformat": "unixtime",
            "timezone": "GMT",
            **params,
        }
        try:
            async with self._session.get(url, params=query, timeout=TIMEOUT) as resp:
                payload = await resp.json(content_type=None)
                if resp.status != 200:
                    reason = payload.get("reason") if isinstance(payload, dict) else None
                    raise OpenMeteoError(f"HTTP {resp.status}: {reason or 'error'}")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise OpenMeteoError(f"{type(err).__name__}: {err}") from err
        if not isinstance(payload, dict):
            raise OpenMeteoError("unexpected response")
        return payload

    async def async_history(self, start: date, end: date) -> dict[datetime, WeatherHour]:
        """Archived forecasts (stitched from the most recent model runs)."""
        payload = await self._get(
            HISTORY_URL,
            # one extra day: the first hour of a day is stamped on the next
            {"start_date": start.isoformat(), "end_date": (end + timedelta(days=1)).isoformat()},
        )
        return parse_hourly(payload)

    async def async_forecast(self, days: int = 3) -> dict[datetime, WeatherHour]:
        payload = await self._get(FORECAST_URL, {"forecast_days": days, "past_days": 1})
        return parse_hourly(payload)
