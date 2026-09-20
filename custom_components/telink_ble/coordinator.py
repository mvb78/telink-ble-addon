"""DataUpdateCoordinator that polls the Telink add-on REST API.

Transport note: the add-on publishes its web UI on TCP 8098 (host network), so
the integration talks to it directly over plain HTTP. This avoids the
Supervisor/Ingress token plumbing and works whether the add-on runs under HAOS,
Docker, or a plain host — the config flow simply needs a reachable host:port.

The add-on already exposes a single-shot "status for ALL lamps" endpoint
(POST /api/command/status with an empty body, via the daemon's selector:all),
so one HTTP request per poll refresh covers every entity.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_DAEMON,
    API_GROUPS,
    API_LAMPS,
    API_STATUS_ALL,
    DAEMON_STATE_TIMEOUT,
    DEFAULT_DAEMON_PORT,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _as_bool(value: Any) -> bool:
    return str(value).lower() in ("1", "true", "yes", "on")


def _common_member_colortemp(on_members: list[dict]) -> int | None:
    """Most common colortemp (warm%) across members with known state.

    After a group command all members converge to the same value, so the
    mode is the group's color temperature. Returns None when unknown.
    """
    counts: dict[int, int] = {}
    for s in on_members:
        ct = s.get("colortemp")
        if ct is None:
            continue
        try:
            ct = int(ct)
        except (TypeError, ValueError):
            continue
        counts[ct] = counts.get(ct, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda c: (counts[c], -abs(c - 50)))


class TelinkCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll lamps, groups and their combined status from the add-on."""

    def __init__(self, hass: HomeAssistant, base_url: str, poll_interval: int,
                 session: aiohttp.ClientSession, daemon_port: int | None = None):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=max(poll_interval, 5)),
        )
        self._base_url = base_url.rstrip("/")
        self._session = session
        self._daemon_port = daemon_port or DEFAULT_DAEMON_PORT

    # -- low-level requests -------------------------------------------------
    async def _request(
        self, method: str, path: str, payload: dict | None = None, total: float = 20
    ) -> Any:
        url = f"{self._base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=total)
        try:
            async with self._session.request(
                method, url, json=payload, timeout=timeout
            ) as resp:
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Add-on request failed for {path}: {err}") from err
        except asyncio.TimeoutError as err:
            raise UpdateFailed(f"Add-on request timed out for {path}") from err
        except ValueError as err:
            # non-JSON body (add-on restarting, error page, empty) — treat as
            # UpdateFailed so a single bad poll can never wedge the coordinator
            # and leave every entity stuck "unavailable". (JSONDecodeError is a
            # ValueError; the old `json.JSONDecodeError` tuple element crashed
            # AttributeError during HA shutdown because the `json` payload
            # parameter shadowed the module name here.)
            raise UpdateFailed(f"Add-on returned non-JSON for {path}") from err

    async def get_lamps(self) -> list[dict]:
        data = await self._request("GET", API_LAMPS, total=10)
        return data if isinstance(data, list) else []

    async def get_groups(self) -> list[dict]:
        data = await self._request("GET", API_GROUPS, total=10)
        return data if isinstance(data, list) else []

    async def get_status_all(self) -> list[dict]:
        data = await self._request("POST", API_STATUS_ALL, payload={}, total=75)
        results = data.get("results") if isinstance(data, dict) else []
        return results if isinstance(results, list) else []

    async def send_command(self, path: str, payload: dict) -> bool:
        # The sidecar daemon may briefly be mid-reconnect; retry failures a
        # couple of times before surfacing the error. Timed-out requests are
        # not retried to avoid double mesh bursts, and "ok": false responses
        # are retried as well since the daemon's own verified send can be
        # racing a keepalive-triggered reconnect.
        last: Exception | None = None
        for attempt in range(1, 4):
            try:
                data = await self._request("POST", path, payload=payload, total=90)
            except UpdateFailed as err:
                last = err
                _LOGGER.warning("Telink command %s failed (attempt %d/3): %s",
                                path, attempt, last)
            else:
                if isinstance(data, dict) and data.get("ok"):
                    return True
                msg = data.get("msg") if isinstance(data, dict) else "invalid add-on response"
                last = HomeAssistantError(f"Add-on rejected {path}: {msg}")
                _LOGGER.warning("Telink command %s rejected (attempt %d/3): %s",
                                path, attempt, msg)
            if attempt < 3:
                await asyncio.sleep(2)
        raise HomeAssistantError(f"Telink command {path} failed after 3 attempts: {last}") from last

    # -- coordinator ---------------------------------------------------------
    async def _fetch_daemon_state(self) -> dict[str, Any] | None:
        """Read the sidecar daemon's evented state cache over raw TCP.

        The daemon keeps each lamp's last-decoded 0xDB status push (the lamp
        notifies after every mesh write, incl. group broadcasts), so this read
        is a cheap in-memory snapshot without any BLE interaction. Returns
        None when the socket is unavailable — the coordinator gracefully
        falls back to the slower /api/command/status polling path.
        """
        parsed = urlparse(self._base_url)
        host = parsed.hostname
        if not host:
            return None
        port = self._daemon_port or DEFAULT_DAEMON_PORT
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=2.0
            )
        except (OSError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Telink daemon TCP %s:%s unreachable: %s", host, port, err)
            return None
        try:
            writer.write(json.dumps({"kind": "state"}).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=DAEMON_STATE_TIMEOUT)
            resp = json.loads(line.decode())
        except (OSError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.debug("Telink daemon state read failed: %s", err)
            return None
        finally:
            with contextlib.suppress(Exception):
                writer.close()
        if resp.get("status") == "ok":
            return resp.get("state") or {}
        return None

    async def _async_update_data(self) -> dict[str, Any]:
        # Individual add-on endpoints may fail (or hang) when the Telink lamps
        # are asleep / not advertising. Rather than fail the whole update (which
        # would trip the config entry into setup_retry), degrade gracefully:
        # keep last-known lamps/groups and mark entities unavailable. All four
        # requests run concurrently so a poll costs ~the slowest one, not the sum.
        lamps = (self.data or {}).get("lamps", [])
        groups = (self.data or {}).get("groups", [])
        connected = (self.data or {}).get("connected", False)
        by_mac: dict[str, dict] = {}
        cached_state = (self.data or {}).get("cached_state") or {}

        async def _fetch_lamps():
            nonlocal lamps
            try:
                lamps = await self.get_lamps()
            except UpdateFailed:
                _LOGGER.debug("Telink lamps fetch failed; keeping last-known")

        async def _fetch_groups():
            nonlocal groups
            try:
                groups = await self.get_groups()
            except UpdateFailed:
                _LOGGER.debug("Telink groups fetch failed; keeping last-known")

        async def _fetch_status():
            try:
                statuses = await self.get_status_all()
                for item in statuses:
                    entry = item.get("result")
                    mac = item.get("mac")
                    if mac and isinstance(entry, dict):
                        by_mac[mac.lower()] = entry
            except UpdateFailed:
                _LOGGER.debug("Telink status poll failed; lamps likely offline")

        async def _fetch_daemon():
            nonlocal connected
            try:
                daemon = await self._request("GET", API_DAEMON, total=10)
                connected = _as_bool(daemon.get("running"))
            except UpdateFailed:
                _LOGGER.debug("Telink daemon liveness check failed; keeping last-known")

        async def _fetch_state():
            nonlocal cached_state
            try:
                fresh = await self._fetch_daemon_state()
                if fresh:
                    cached_state = {k.lower(): v for k, v in fresh.items()}
            except Exception:  # noqa: BLE001 — state cache is best-effort
                _LOGGER.debug("Telink daemon state fetch failed; keeping last-known")

        await asyncio.gather(_fetch_lamps(), _fetch_groups(), _fetch_status(),
                             _fetch_daemon(), _fetch_state())

        # Compose group truth: a mesh group has no read-back, but every member
        # lamp pushes its 0xDB status through its own session, so the OR over
        # known members is the real group state.
        group_states: dict[int, dict] = {}
        for group in groups or []:
            addr = group.get("address")
            if not addr:
                continue
            members = {str(m).upper() for m in (group.get("lamps") or [])}
            known = [cached_state[m.lower()]
                     for m in members if m.lower() in cached_state]
            if known and not all(s.get("unknown") for s in known):
                on_members = [s for s in known if not s.get("unknown")]
                group_states[addr] = {
                    "on": any(s.get("on") for s in on_members),
                    "brightness": max((s.get("brightness") or 0) for s in on_members),
                    "colortemp": _common_member_colortemp(on_members),
                    "unknown_mask": [m for m in members
                                     if m.lower() not in cached_state],
                }

        return {
            "lamps": lamps,
            "groups": groups,
            "status": by_mac,
            "connected": connected,
            "cached_state": cached_state,
            "group_states": group_states,
        }
