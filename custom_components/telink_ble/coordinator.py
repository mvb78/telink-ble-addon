"""DataUpdateCoordinator that polls the Telink add-on REST API.

Transport note: the add-on publishes its web UI on TCP 8099 (host network), so
the integration talks to it directly over plain HTTP. This avoids the
Supervisor/Ingress token plumbing and works whether the add-on runs under HAOS,
Docker, or a plain host — the config flow simply needs a reachable host:port.

The add-on already exposes a single-shot "status for ALL lamps" endpoint
(POST /api/command/status with an empty body, via the daemon's selector:all),
so one HTTP request per poll refresh covers every entity.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_DAEMON,
    API_GROUPS,
    API_LAMPS,
    API_STATUS_ALL,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _as_bool(value: Any) -> bool:
    return str(value).lower() in ("1", "true", "yes", "on")


class TelinkCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll lamps, groups and their combined status from the add-on."""

    def __init__(self, hass: HomeAssistant, base_url: str, poll_interval: int,
                 session: aiohttp.ClientSession):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=max(poll_interval, 5)),
        )
        self._base_url = base_url.rstrip("/")
        self._session = session

    # -- low-level requests -------------------------------------------------
    async def _request(self, method: str, path: str, json: dict | None = None) -> Any:
        url = f"{self._base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with self._session.request(
                method, url, json=json, timeout=timeout
            ) as resp:
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Add-on request failed for {path}: {err}") from err
        except asyncio.TimeoutError as err:
            raise UpdateFailed(f"Add-on request timed out for {path}") from err

    async def get_lamps(self) -> list[dict]:
        data = await self._request("GET", API_LAMPS)
        return data if isinstance(data, list) else []

    async def get_groups(self) -> list[dict]:
        data = await self._request("GET", API_GROUPS)
        return data if isinstance(data, list) else []

    async def get_status_all(self) -> list[dict]:
        data = await self._request("POST", API_STATUS_ALL, json={})
        results = data.get("results") if isinstance(data, dict) else []
        return results if isinstance(results, list) else []

    async def send_command(self, path: str, payload: dict) -> bool:
        data = await self._request("POST", path, json=payload)
        return isinstance(data, dict) and bool(data.get("ok"))

    # -- coordinator ---------------------------------------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        lamps = await self.get_lamps()
        groups = await self.get_groups()
        statuses = await self.get_status_all()

        by_mac: dict[str, dict] = {}
        for item in statuses:
            entry = item.get("result")
            mac = item.get("mac")
            if mac and isinstance(entry, dict):
                by_mac[mac.lower()] = entry

        daemon = await self._request("GET", API_DAEMON)
        return {
            "lamps": lamps,
            "groups": groups,
            "status": by_mac,
            "connected": _as_bool(daemon.get("running")),
        }
