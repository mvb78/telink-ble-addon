"""Light platform for Telink BLE lamps and groups.

Each discovered lamp becomes a `light` entity with real polled state (the add-on
returns per-lamp status through its daemon query-proxy). Each group becomes a
single `light` entity that controls every member via a one-packet mesh broadcast
to the group address; the mesh does not report group state, so group entities
use assumed state (tracked from commands sent here).

Lamp units:
  * brightness  - add-on 0-100  <->  HA 0-255
  * color_temp  - add-on 0-100  (0=cool/6500K..100=warm/2700K)
                 <->  HA color_temp_kelvin (6500..2700 K)
Physical config: tunable white only (proven on hardware) -> COLOR_TEMP mode, no RGB.
"""

from __future__ import annotations

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
    API_CMD_BRIGHTNESS,
    API_CMD_COLORTEMP,
    API_CMD_OFF,
    API_CMD_ON,
    DOMAIN,
    KELVIN_SPAN,
    MAX_COLOR_TEMP_KELVIN,
    MIN_COLOR_TEMP_KELVIN,
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


def warmth_to_kelvin(warmth: int) -> int:
    """Map add-on 0-100 (0=cool,100=warm) -> colour temperature in Kelvin."""
    return round(MAX_COLOR_TEMP_KELVIN - KELVIN_SPAN * warmth / VAL_MAX)


def kelvin_to_warmth(kelvin: int) -> int:
    """Map HA colour-temperature Kelvin -> add-on 0-100 warmth value."""
    return round((MAX_COLOR_TEMP_KELVIN - kelvin) * VAL_MAX / KELVIN_SPAN)


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
    # Kelvin API (mired attrs were removed in Core 2026.3).
    _attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN   # warmest
    _attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN   # coldest

    def __init__(self, coordinator: TelinkCoordinator, payload: dict):
        super().__init__(coordinator)
        self._target = payload

    @property
    def available(self) -> bool:
        coordinator_data = self.coordinator.data
        return bool(
            coordinator_data
            and coordinator_data.get("connected")
        )

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

    @property
    def _status(self) -> dict | None:
        data = self.coordinator.data or {}
        return data.get("status", {}).get(self._mac)

    @property
    def is_on(self) -> bool | None:
        status = self._status
        if status is None:
            return None
        return status.get("state") == "ON"

    @property
    def brightness(self) -> int | None:
        status = self._status
        if status is None:
            return None
        return brightness_val_to_ha(int(status.get("brightness", 0)))

    @property
    def color_temp_kelvin(self) -> int | None:
        status = self._status
        if status is None:
            return None
        return warmth_to_kelvin(int(status.get("colortemp", 0)))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.send_command(API_CMD_ON, {"mac": self._mac})
        if kwargs.get(ATTR_BRIGHTNESS) is not None:
            await self.coordinator.send_command(
                API_CMD_BRIGHTNESS, {"mac": self._mac,
                                     "value": brightness_ha_to_val(int(kwargs[ATTR_BRIGHTNESS]))}
            )
        if kwargs.get(ATTR_COLOR_TEMP_KELVIN) is not None:
            await self.coordinator.send_command(
                API_CMD_COLORTEMP, {"mac": self._mac,
                                    "value": kelvin_to_warmth(int(kwargs[ATTR_COLOR_TEMP_KELVIN]))}
            )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.send_command(API_CMD_OFF, {"mac": self._mac})
        await self.coordinator.async_request_refresh()


class TelinkGroupLight(_TelinkBaseLight):
    """A mesh group, controlled via a single packet to the group address.

    The mesh does not expose group state, so this is an assumed-state entity.
    """

    _attr_assumed_state = True

    def __init__(self, coordinator: TelinkCoordinator, group: dict):
        super().__init__(coordinator, group)
        self._addr = int(group["address"])
        self._name = group["name"]
        self._attr_unique_id = f"telink_ble_group_{self._addr}"
        self._attr_name = f"Telink {self._name}"
        self._on = False
        self._brightness: int | None = None
        self._color_temp_kelvin: int | None = None

    @property
    def is_on(self) -> bool:
        return self._on

    @property
    def brightness(self) -> int | None:
        return self._brightness

    @property
    def color_temp_kelvin(self) -> int | None:
        return self._color_temp_kelvin

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.send_command(API_CMD_ON, {"dst": self._addr})
        if kwargs.get(ATTR_BRIGHTNESS) is not None:
            await self.coordinator.send_command(
                API_CMD_BRIGHTNESS, {"dst": self._addr,
                                     "value": brightness_ha_to_val(int(kwargs[ATTR_BRIGHTNESS]))}
            )
            self._brightness = int(kwargs[ATTR_BRIGHTNESS])
        if kwargs.get(ATTR_COLOR_TEMP_KELVIN) is not None:
            await self.coordinator.send_command(
                API_CMD_COLORTEMP, {"dst": self._addr,
                                    "value": kelvin_to_warmth(int(kwargs[ATTR_COLOR_TEMP_KELVIN]))}
            )
            self._color_temp_kelvin = int(kwargs[ATTR_COLOR_TEMP_KELVIN])
        self._on = True
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.send_command(API_CMD_OFF, {"dst": self._addr})
        self._on = False
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Group has no read-back; keep local assumed state only."""
        self.async_write_ha_state()
