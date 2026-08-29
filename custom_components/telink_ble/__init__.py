"""Telink BLE Lights integration — Home Assistant companion to the telink add-on.

The add-on (telink-ble-cli) owns the BLE connections and exposes a REST API.
This integration bridges that API to native HA `light` entities (one per lamp
and one per group), polling bulk status through the add-on's daemon query-proxy.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.typing import ConfigType

from .const import CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_SECONDS, DOMAIN
from .coordinator import TelinkCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LIGHT]

DATA_COORDINATOR = "coordinator"
DATA_SESSION = "session"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (no YAML configuration)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the coordinator and light entities from a config entry."""
    host = entry.data.get(CONF_HOST, "")
    port = int(entry.data.get(CONF_PORT, 8099))
    poll_interval = int(
        (entry.options or {}).get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_SECONDS)
    )
    base_url = f"http://{host}:{port}"

    session = aiohttp_client.async_get_clientsession(hass)
    coordinator = TelinkCoordinator(hass, base_url, poll_interval, session)

    # First refresh doubles as a connectivity check. If the add-on is down we
    # surface a retryable error (supervisor shows "not ready") rather than
    # silently creating dead entities.
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as exc:  # noqa: BLE001
        raise ConfigEntryNotReady(f"Add-on at {base_url} not reachable: {exc}") from exc

    hass.data[DOMAIN].setdefault(entry.entry_id, {})[DATA_COORDINATOR] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
