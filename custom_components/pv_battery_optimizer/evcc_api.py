"""Minimal async client for the evcc REST API."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=10)


class EvccError(Exception):
    """Communication error."""


class EvccAuthError(EvccError):
    """Authentication failed."""


@dataclass(frozen=True)
class Rate:
    start: datetime
    end: datetime
    value: float


@dataclass(frozen=True)
class LoadpointInfo:
    mode: str | None
    charging: bool
    charge_power_w: float
    plan_slots: list[tuple[datetime, datetime]] = field(default_factory=list)


@dataclass(frozen=True)
class LiveState:
    soc_percent: float | None
    capacity_kwh: float | None
    battery_mode: str | None
    loadpoints: list[LoadpointInfo]

    @property
    def ev_charging(self) -> bool:
        return any(lp.charging for lp in self.loadpoints)


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def _unwrap(data: Any) -> Any:
    # older evcc versions wrap responses in {"result": ...}
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


def parse_state(state: dict[str, Any]) -> LiveState:
    battery = state.get("battery")
    if isinstance(battery, dict):
        soc = battery.get("soc")
        capacity = battery.get("capacity")
    else:  # older evcc versions
        soc = state.get("batterySoc")
        capacity = state.get("batteryCapacity")

    loadpoints: list[LoadpointInfo] = []
    for lp in state.get("loadpoints") or []:
        slots: list[tuple[datetime, datetime]] = []
        for item in lp.get("plan") or []:
            start, end = _parse_dt(item.get("start")), _parse_dt(item.get("end"))
            if start and end:
                slots.append((start, end))
        loadpoints.append(
            LoadpointInfo(
                mode=lp.get("mode"),
                charging=bool(lp.get("charging")),
                charge_power_w=float(lp.get("chargePower") or 0.0),
                plan_slots=slots,
            )
        )

    return LiveState(
        soc_percent=float(soc) if soc is not None else None,
        capacity_kwh=float(capacity) if capacity else None,
        battery_mode=state.get("batteryMode"),
        loadpoints=loadpoints,
    )


class EvccClient:
    """evcc REST API client.

    Authentication uses the ``auth`` cookie returned by ``/api/auth/login``.
    The cookie is sent explicitly because aiohttp's default cookie jar ignores
    cookies from IP-address hosts.
    """

    def __init__(
        self, session: aiohttp.ClientSession, base_url: str, password: str | None
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._password = password or None
        self._token: str | None = None

    async def login(self) -> None:
        if not self._password:
            raise EvccAuthError("evcc requires a password")
        try:
            async with self._session.post(
                f"{self._base}/api/auth/login",
                json={"password": self._password},
                timeout=TIMEOUT,
            ) as resp:
                if resp.status == 401:
                    raise EvccAuthError("invalid evcc password")
                resp.raise_for_status()
                cookie = resp.cookies.get("auth")
                self._token = cookie.value if cookie else None
        except aiohttp.ClientError as err:
            raise EvccError(f"evcc login failed: {err}") from err

    async def _request(
        self, method: str, path: str, *, allow_404: bool = False, retry: bool = True
    ) -> Any:
        headers = {"Cookie": f"auth={self._token}"} if self._token else {}
        try:
            async with self._session.request(
                method, f"{self._base}/api/{path}", headers=headers, timeout=TIMEOUT
            ) as resp:
                if resp.status == 401 and retry and self._password:
                    await self.login()
                    return await self._request(method, path, allow_404=allow_404, retry=False)
                if resp.status == 401:
                    raise EvccAuthError("evcc authentication required")
                if resp.status == 404 and allow_404:
                    return None
                resp.raise_for_status()
                text = await resp.text()
        except aiohttp.ClientError as err:
            raise EvccError(f"evcc request {method} {path} failed: {err}") from err
        # don't rely on the Content-Type header
        try:
            return _unwrap(json.loads(text)) if text else None
        except ValueError:
            return text

    async def get_state(self) -> dict[str, Any]:
        return await self._request("GET", "state")

    async def get_tariff(self, kind: str) -> list[Rate] | None:
        """Return rates for tariff ``kind`` (grid, feedin, ...) or None."""
        data = await self._request("GET", f"tariff/{kind}", allow_404=True)
        if not data:
            return None
        rates: list[Rate] = []
        for item in data.get("rates") or []:
            start, end = _parse_dt(item.get("start")), _parse_dt(item.get("end"))
            value = item.get("value", item.get("price"))
            if start and end and value is not None:
                rates.append(Rate(start, end, float(value)))
        rates.sort(key=lambda r: r.start)
        return rates

    async def set_battery_mode(self, mode: str) -> None:
        """Set external battery mode. evcc resets it after 60 s, so this has
        to be called periodically."""
        await self._request("POST", f"batterymode/{mode}")

    async def clear_battery_mode(self) -> None:
        """Hand battery control back to evcc."""
        await self._request("DELETE", "batterymode")
