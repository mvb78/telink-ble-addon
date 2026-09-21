"""Light platform for Telink BLE lamps and groups.

Each discovered lamp becomes a `light` entity with real polled state (the add-on
returns per-lamp status through its daemon query-proxy). Each group becomes a
single `light` entity that controls every member via a one-packet mesh broadcast
to the group address; the mesh does not report group state, so group entities
use assumed state (tracked from commands sent here).

Lamp units (tunable white only — verified on hardware):
  * brightness       - add-on 0-100  <->  HA 0-255
  * colortemp        - add-on 0..100 warm% <-> HA 2700..6500 K (COLOR_TEMP)
    HW ct 0=warm(2700K) 100=cool(6500K); add-on colortemp value = warm% (0=cool,100=warm)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    API_CMD_SET,
    DOMAIN,
    VAL_MAX,
    VAL_MIN,
)
from .coordinator import TelinkCoordinator

_LOGGER = logging.getLogger(__name__)


def brightness_ha_to_val(ha: int) -> int:
    """Map HA 0-255 -> add-on 0-100."""
    return round(max(VAL_MIN, min(VAL_MAX, ha * VAL_MAX / 255)))


def brightness_val_to_ha(val: int) -> int:
    """Map add-on 0-100 -> HA 0-255."""
    return round(max(0, min(255, val * 255 / VAL_MAX)))


# Kelvin 2700 (warm) <-> 6500 (cool) <-> add-on warm% 0..100 (0=cool 100=warm)
KELVIN_MIN = 2700
KELVIN_MAX = 6500


def kelvin_to_warm_pct(kelvin: int) -> int:
    """HA Kelvin -> add-on 0..100 warm%."""
    kelvin = max(KELVIN_MIN, min(KELVIN_MAX, kelvin))
    # warm% = (6500 - K) / 38  → 6500=>0, 2700=>100
    return round((KELVIN_MAX - kelvin) * 100 / (KELVIN_MAX - KELVIN_MIN))


def warm_pct_to_kelvin(pct: int) -> int:
    """Add-on 0..100 warm% -> HA Kelvin."""
    pct = max(VAL_MIN, min(VAL_MAX, pct))
    return round(KELVIN_MAX - pct * (KELVIN_MAX - KELVIN_MIN) / 100)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Telink lights from a config entry."""
    coordinator: TelinkCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities: list[LightEntity] = []
    data = coordinator.data or {}
    for lamp in data.get("lamps", []):
        entities.append(TelinkLampLight(coordinator, lamp))
    for group in data.get("groups", []):
        entities.append(TelinkGroupLight(coordinator, group))

    async_add_entities(entities)


class _TelinkBaseLight(CoordinatorEntity, LightEntity):
    """Shared behaviour for Telink lamp/group lights."""

    _attr_has_entity_name = False
    _attr_color_mode = ColorMode.COLOR_TEMP
    _attr_supported_color_modes = {ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = KELVIN_MIN
    _attr_max_color_temp_kelvin = KELVIN_MAX

    def __init__(self, coordinator: TelinkCoordinator, payload: dict):
        super().__init__(coordinator)
        self._target = payload

    @property
    def available(self) -> bool:
        # Default CoordinatorEntity behaviour: available while the add-on
        # responds. The add-on degrades gracefully (keeps last-known data on a
        # failed poll), so a single failed poll must NOT make lights
        # unavailable — that previously caused automations to no-op for hours.
        return self.coordinator.last_update_success

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self.name,
            "manufacturer": "Telink",
            "model": "Smart_qXsx",
        }


class TelinkLampLight(_TelinkBaseLight):
    """A physical lamp, state read from the coordinator's polled status."""

    def __init__(self, coordinator: TelinkCoordinator, lamp: dict):
        super().__init__(coordinator, lamp)
        self._mac = lamp["mac"].lower()
        self._name = lamp.get("name") or lamp["mac"]
        self._attr_unique_id = f"telink_ble_{self._mac}"
        self._attr_name = f"Telink {self._name}"
        self._attr_should_poll = False
        # Optimistic overrides applied right after a command; cleared when the
        # next polled status arrives, so state responds instantly to controls.
        self._opt_on: bool | None = None
        self._opt_brightness: int | None = None
        self._opt_color_temp: int | None = None

    @property
    def _status(self) -> dict | None:
        data = self.coordinator.data or {}
        return data.get("status", {}).get(self._mac)

    @property
    def is_on(self) -> bool | None:
        if self._opt_on is not None:
            return self._opt_on
        status = self._status
        if status is None:
            return None
        # These lamps always report state:"ON" — "off" is brightness 0.
        return int(status.get("brightness", 0)) > 0

    @property
    def brightness(self) -> int | None:
        if self._opt_brightness is not None:
            return self._opt_brightness
        status = self._status
        if status is None:
            return None
        return brightness_val_to_ha(int(status.get("brightness", 0)))

    @property
    def color_temp_kelvin(self) -> int | None:
        if self._opt_color_temp is not None:
            return self._opt_color_temp
        status = self._status
        if status is None:
            return None
        # status colortemp is warm% 0..100, None means white mode without CT
        pct = status.get("colortemp")
        if pct is None:
            return None
        return warm_pct_to_kelvin(int(pct))

    async def async_turn_on(self, **kwargs: Any) -> None:
        payload: dict[str, Any] = {"mac": self._mac, "on": True}
        if kwargs.get(ATTR_BRIGHTNESS) is not None:
            ha_b = int(kwargs[ATTR_BRIGHTNESS])
            payload["brightness"] = brightness_ha_to_val(ha_b)
            self._opt_brightness = ha_b
        if kwargs.get(ATTR_COLOR_TEMP_KELVIN) is not None:
            k = int(kwargs[ATTR_COLOR_TEMP_KELVIN])
            payload["colortemp"] = kelvin_to_warm_pct(k)
            self._opt_color_temp = k
        await self.coordinator.send_command(API_CMD_SET, payload)
        self._opt_on = True
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.send_command(API_CMD_SET, {"mac": self._mac, "on": False})
        self._opt_on = False
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        # Fresh polled status is now authoritative — drop optimistic overrides.
        self._opt_on = None
        self._opt_brightness = None
        self._opt_color_temp = None
        self.async_write_ha_state()


class TelinkGroupLight(_TelinkBaseLight):
    """A mesh group that acts as ONE lamp.

    Control goes out as a single mesh broadcast to the group address (fast
    path — most members follow at once), then member states from the daemon
    push cache are compared against the target and any drifted member gets a
    direct unicast re-send (bounded). The entity therefore converges to one
    uniform on/brightness/colortemp across all 3 members instead of showing
    a mixed mesh.
    """

    # Tolerances when comparing member push state to the commanded target
    # (add-on units: brightness 0-100, colortemp warm% 0-100).
    _SYNC_BRI_TOL = 3
    _SYNC_CT_TOL = 4
    _SYNC_ROUNDS = 1

    def __init__(self, coordinator: TelinkCoordinator, group: dict):
        super().__init__(coordinator, group)
        self._addr = int(group["address"])
        self._name = group["name"]
        self._attr_unique_id = f"telink_ble_group_{self._addr}"
        self._attr_name = f"Telink {self._name}"
        self._members = [str(m).upper() for m in (group.get("lamps") or [])]
        self._on = False
        self._brightness: int | None = None
        self._color_temp_kelvin: int | None = None

    @property
    def _group_state(self) -> dict | None:
        data = self.coordinator.data or {}
        return (data.get("group_states") or {}).get(self._addr)

    @property
    def is_on(self) -> bool:
        state = self._group_state
        if state is not None:
            return bool(state.get("on"))
        return self._on

    @property
    def brightness(self) -> int | None:
        state = self._group_state
        if state is not None and state.get("on"):
            return brightness_val_to_ha(int(state.get("brightness") or 0))
        if state is not None:
            return 0
        return self._brightness

    @property
    def color_temp_kelvin(self) -> int | None:
        state = self._group_state
        if state is not None and state.get("colortemp") is not None:
            return warm_pct_to_kelvin(int(state["colortemp"]))
        return self._color_temp_kelvin

    @property
    def assumed_state(self) -> bool:
        # Real member truth available → drop the assumed-state marker so dashboards
        # show authentic state after restarts; otherwise keep optimistic UX.
        return self._group_state is None

    async def async_turn_on(self, **kwargs: Any) -> None:
        payload: dict[str, Any] = {"dst": self._addr, "on": True}
        if kwargs.get(ATTR_BRIGHTNESS) is not None:
            ha_b = int(kwargs[ATTR_BRIGHTNESS])
            payload["brightness"] = brightness_ha_to_val(ha_b)
            self._brightness = ha_b
        if kwargs.get(ATTR_COLOR_TEMP_KELVIN) is not None:
            k = int(kwargs[ATTR_COLOR_TEMP_KELVIN])
            payload["colortemp"] = kelvin_to_warm_pct(k)
            self._color_temp_kelvin = k
        await self.coordinator.send_command(API_CMD_SET, payload)
        self._on = True
        self.async_write_ha_state()
        await asyncio.sleep(3)
        await self.coordinator.async_refresh_state_cache()
        await self._sync_members(on=True, brightness_ha=self._brightness,
                                 kelvin=self._color_temp_kelvin)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.send_command(API_CMD_SET, {"dst": self._addr, "on": False})
        self._on = False
        self.async_write_ha_state()
        await asyncio.sleep(3)
        await self.coordinator.async_refresh_state_cache()
        await self._sync_members(on=False)
        self.async_write_ha_state()

    async def _sync_members(self, *, on: bool, brightness_ha: int | None = None,
                            kelvin: int | None = None) -> None:
        """Force every member lamp to the commanded group state.

        Compares each member's daemon push cache against the target and
        re-sends a direct unicast to drifted members. Bounded to
        _SYNC_ROUNDS so a dead lamp can't stall the service call.
        """
        if not self._members:
            return
        target_bri = (brightness_ha_to_val(int(brightness_ha))
                      if brightness_ha is not None else None)
        target_ct = kelvin_to_warm_pct(int(kelvin)) if kelvin is not None else None
        for _ in range(self._SYNC_ROUNDS):
            cached = (self.coordinator.data or {}).get("cached_state") or {}
            drifted: list[str] = []
            for mac in self._members:
                st = cached.get(mac.lower())
                if not st or st.get("unknown"):
                    continue  # no truth for this member — leave it alone
                if not on:
                    if st.get("on"):
                        drifted.append(mac)
                    continue
                ok_b = (target_bri is None
                        or abs(int(st.get("brightness") or 0) - target_bri) <= self._SYNC_BRI_TOL)
                ok_c = (target_ct is None or st.get("colortemp") is None
                        or abs(int(st.get("colortemp")) - target_ct) <= self._SYNC_CT_TOL)
                if not (st.get("on") and ok_b and ok_c):
                    drifted.append(mac)
            if not drifted:
                return
            _LOGGER.debug("Telink group %s: re-syncing drifted members %s",
                          self._name, drifted)
            for mac in drifted:
                payload: dict[str, Any] = {"mac": mac, "on": on}
                if on:
                    if brightness_ha is not None:
                        payload["brightness"] = brightness_ha_to_val(int(brightness_ha))
                    if kelvin is not None:
                        payload["colortemp"] = kelvin_to_warm_pct(int(kelvin))
                try:
                    await self.coordinator.send_command(API_CMD_SET, payload)
                except Exception as err:  # noqa: BLE001 — one dead lamp must not fail the group
                    _LOGGER.warning("Telink group %s: member %s re-sync failed: %s",
                                    self._name, mac, err)
            await asyncio.sleep(3)
            # Lightweight refresh (push cache only, no BLE status poll) so
            # sync rounds cost seconds, not the ~60s+ of a full refresh.
            await self.coordinator.async_refresh_state_cache()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Coordinator refreshes bring real composed group state."""
        self.async_write_ha_state()
