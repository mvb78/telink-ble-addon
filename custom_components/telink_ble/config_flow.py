"""Config flow for the Telink BLE Lights integration.

The add-on publishes its web UI on TCP 8098 in the host network namespace, so
the integration needs a reachable host:port. When Home Assistant runs under the
HAOS Supervisor (or any Docker host) this is normally the HA host's own LAN IP
or `host.docker.internal`. The flow probes a set of candidates automatically and
falls back to manual entry.
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import aiohttp_client

from .const import (
    API_DAEMON,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_PORT,
    DOMAIN,
)

CONNECTION_TEST_TIMEOUT = 5


async def _host_is_responsive(hass: HomeAssistant, host: str, port: int) -> bool:
    """Return True if host:port answers the add-on's /api/daemon probe."""
    url = f"http://{host}:{port}{API_DAEMON}"
    session = aiohttp_client.async_get_clientsession(hass)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(
                total=CONNECTION_TEST_TIMEOUT)) as resp:
            if resp.status != 200:
                return False
            data = await resp.json()
            return isinstance(data, dict) and "running" in data
    except (aiohttp.ClientError, ValueError, asyncio.TimeoutError):
        return False


async def _candidate_hosts(hass: HomeAssistant) -> list[str]:
    """Return likely hosts in order of preference."""
    candidates: list[str] = []
    api = getattr(hass, "config", None) and getattr(hass.config, "api", None)
    if api and getattr(api, "host", None):
        candidates.append(api.host)
    candidates.append("host.docker.internal")
    candidates.append("127.0.0.1")
    # De-duplicate while preserving order.
    seen: set[str] = set()
    return [c for c in candidates if not (c in seen or seen.add(c))]


class TelinkBleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Telink BLE Lights add-on."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            if await _host_is_responsive(self.hass, host, port):
                return self.async_create_entry(
                    title=f"Telink BLE ({host}:{port})",
                    data={CONF_HOST: host, CONF_PORT: port},
                    options={CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL_SECONDS},
                )
            errors["base"] = "cannot_connect"

        detected = await _candidate_hosts(self.hass)

        data_schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=detected[0] if detected else ""): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return TelinkBleOptionsFlow(config_entry)


class TelinkBleOptionsFlow(config_entries.OptionsFlow):
    """Options flow for adjusting connection settings."""

    def __init__(self, config_entry: config_entries.ConfigEntry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        current = self.config_entry.data
        current_opts = self.config_entry.options or {}
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            if await _host_is_responsive(self.hass, host, port):
                new_data = {**current, CONF_HOST: host, CONF_PORT: port}
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data,
                    options={**current_opts, CONF_POLL_INTERVAL: user_input[CONF_POLL_INTERVAL]},
                )
                await self.hass.config_entries.async_reload(self.config_entry.entry_id)
                return self.async_create_entry(title="", data={})
            errors["base"] = "cannot_connect"

        data_schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=current.get(CONF_HOST, "")): str,
                vol.Required(CONF_PORT, default=current.get(CONF_PORT, DEFAULT_PORT)): int,
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=current_opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_SECONDS),
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=3600)),
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=data_schema, errors=errors,
        )
