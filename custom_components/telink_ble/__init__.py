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

    # ── services for full white-only control beyond on/off ──────────────

    async def _handle_recall_scene(call):
        scene_id = int(call.data.get("scene_id", 1))
        dst = call.data.get("dst")
        # entity target → resolve via coordinator lamp/group list if needed
        payload: dict[str, Any] = {}
        if dst is not None:
            payload["dst"] = int(dst)
        elif call.data.get("entity_id"):
            # if a light entity was targeted, forward its mac/dst via coordinator lookup
            ent_ids = call.data["entity_id"]
            if isinstance(ent_ids, str):
                ent_ids = [ent_ids]
            # use first entity's underlying mac/dst if we can
            for eid in ent_ids:
                for lamp in (coordinator.data or {}).get("lamps", []):
                    if f"telink_ble_{lamp['mac'].lower()}" in eid:
                        payload["mac"] = lamp["mac"]
                        break
                if "mac" not in payload:
                    for grp in (coordinator.data or {}).get("groups", []):
                        if f"telink_ble_group_{grp['address']}" in eid:
                            payload["dst"] = int(grp["address"])
                            break
                if payload:
                    break
        await coordinator.send_command("/api/command/scene", {"id": scene_id, **payload})

    async def _handle_store_scene(call):
        scene_id = int(call.data.get("scene_id", 1))
        bri = int(call.data.get("brightness", 50))
        ct_k = int(call.data.get("color_temp_kelvin", 4000))
        # warm% for storage
        from .light import kelvin_to_warm_pct

        payload: dict[str, Any] = {
            "id": scene_id,
            "brightness": max(0, min(100, bri)),
            "r": 0, "g": 0, "b": 0,
            "ct": kelvin_to_warm_pct(ct_k),
        }
        await coordinator.send_command("/api/command/scene-add", payload)

    async def _handle_delete_scene(call):
        sid = int(call.data.get("scene_id", 1))
        if sid == 255:
            await coordinator.send_command("/api/command/scene-clear", {})
        else:
            await coordinator.send_command("/api/command/scene-del", {"id": sid})

    async def _handle_sync_time(call):
        await coordinator.send_command("/api/command/time", {})

    hass.services.async_register(DOMAIN, "recall_scene", _handle_recall_scene)
    hass.services.async_register(DOMAIN, "store_scene", _handle_store_scene)
    hass.services.async_register(DOMAIN, "delete_scene", _handle_delete_scene)
    hass.services.async_register(DOMAIN, "sync_time", _handle_sync_time)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    for svc in ("recall_scene", "store_scene", "delete_scene", "sync_time"):
        try:
            hass.services.async_remove(DOMAIN, svc)
        except Exception:
            pass
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
