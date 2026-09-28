"""Switch (control) entities for UniFi Network+.

Unlike every other platform in this integration, these entities write to
the controller instead of only reading from it. Kept in their own file and
deliberately small in scope:

- ``LocateSwitch``: toggles a device's locate (blink LED) mode. Purely
  cosmetic, fully reversible, no functional effect on the device.
- ``PoePortSwitch``: toggles a switch port's PoE mode between "off" and
  "auto". This can disconnect whatever is actually plugged into that port
  (an AP, a camera, ...) - disabled by default like the other per-port
  entities, and the request shape was cross-checked against aiounifi's
  ``DeviceSetPoePortModeRequest`` (see api.py) rather than guessed.
- ``BlockClientSwitch``: blocks/unblocks a client from the network
  entirely (the classic UniFi parental-control/access-control action).
  Disabled by default, same reasoning as PoePortSwitch - this is a
  deliberate per-client action, not something to have 90-odd of enabled
  out of the box.
- ``PortEnabledSwitch``: enables/disables a switch port's forwarding
  entirely - disabled by default, same reasoning as PoePortSwitch (cuts
  off whatever's on that port, just at the link level instead of power).
- ``DeviceLedSwitch``: on/off/default LED override - the low-risk,
  cosmetic-only one of this bunch, enabled by default like LocateSwitch.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RUNTIME_COORDINATOR
from .const import DOMAIN, MANUFACTURER
from .coordinator import UniFiNetworkPlusCoordinator
from .device import controller_via_device_id

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up locate + PoE-port control switches (discovered dynamically,
    same pattern as sensor.py/binary_sensor.py).
    """
    coordinator: UniFiNetworkPlusCoordinator = hass.data[DOMAIN][entry.entry_id][RUNTIME_COORDINATOR]

    known_device_macs: set[str] = set()
    known_poe_port_keys: set[tuple[str, int]] = set()
    known_enable_port_keys: set[tuple[str, int]] = set()
    known_client_macs: set[str] = set()

    def _discover_new_entities() -> None:
        if not coordinator.data:
            return
        new_entities: list[CoordinatorEntity] = []
        for device in coordinator.data.devices:
            if device.mac not in known_device_macs:
                known_device_macs.add(device.mac)
                new_entities.append(LocateSwitch(entry, coordinator, device.mac))
                if device.led_override is not None:
                    new_entities.append(DeviceLedSwitch(entry, coordinator, device.mac))
            for port in device.ports:
                key = (device.mac, port.port_idx)
                if port.poe_mode is not None and key not in known_poe_port_keys:
                    known_poe_port_keys.add(key)
                    new_entities.append(PoePortSwitch(entry, coordinator, device.mac, port.port_idx))
                if port.port_enabled is not None and key not in known_enable_port_keys:
                    known_enable_port_keys.add(key)
                    new_entities.append(PortEnabledSwitch(entry, coordinator, device.mac, port.port_idx))
        for client in coordinator.data.tracked_clients:
            if client.mac not in known_client_macs:
                known_client_macs.add(client.mac)
                new_entities.append(BlockClientSwitch(entry, coordinator, client.mac))
        if new_entities:
            async_add_entities(new_entities)

    _discover_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_discover_new_entities))


class _DeviceControlBase(CoordinatorEntity[UniFiNetworkPlusCoordinator], SwitchEntity):
    """Shared device lookup/device_info for both switch types here."""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator, device_mac: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_mac = device_mac

    def _find_device(self):
        if not self.coordinator.data:
            return None
        for device in self.coordinator.data.devices:
            if device.mac == self._device_mac:
                return device
        return None

    @property
    def device_info(self) -> dict[str, Any]:
        device = self._find_device()
        return {
            "identifiers": {(DOMAIN, f"{self._entry.entry_id}_{self._device_mac}")},
            "name": device.name if device else self._device_mac,
            "manufacturer": MANUFACTURER,
            "model": (device.model if device else None) or "UniFi Device",
            "via_device_id": controller_via_device_id(self.hass, self._entry.entry_id),
        }

    @property
    def _has_control_permission(self) -> bool:
        """False only when the account's site role is *known* and isn't
        "admin" - if the role couldn't be determined at all (None), fail
        open and let the write attempt itself surface any real error,
        rather than hiding the entity over an inconclusive check.
        """
        role = self.coordinator.client.site_role
        return role is None or role == "admin"

    @property
    def available(self) -> bool:
        return super().available and self._find_device() is not None and self._has_control_permission


class LocateSwitch(_DeviceControlBase):
    """Blink a device's status LED to help find it - purely cosmetic."""

    _attr_translation_key = "locate"
    _attr_icon = "mdi:map-marker-radius"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator, device_mac: str) -> None:
        super().__init__(entry, coordinator, device_mac)
        self._attr_unique_id = f"{entry.entry_id}_{device_mac}_locate"

    @property
    def is_on(self) -> bool | None:
        device = self._find_device()
        return device.locating if device else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.client.set_locate(self._device_mac, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.client.set_locate(self._device_mac, False)
        await self.coordinator.async_request_refresh()


class PoePortSwitch(_DeviceControlBase):
    """Turn PoE power on ("auto") or off for one switch port.

    Disabled by default: flipping the wrong port cuts power to whatever is
    plugged into it (an AP, a camera, ...), so this should only be enabled
    deliberately for a specific known port, same as the other per-port
    entities (Link Speed, PoE Power, Download, Upload).
    """

    _attr_translation_key = "port_poe_switch"
    _attr_icon = "mdi:ethernet"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: UniFiNetworkPlusCoordinator,
        device_mac: str,
        port_idx: int,
    ) -> None:
        super().__init__(entry, coordinator, device_mac)
        self._port_idx = port_idx
        self._attr_unique_id = f"{entry.entry_id}_{device_mac}_port{port_idx}_poe_switch"
        port = self._find_port()
        self._attr_translation_placeholders = {"port": port.name if port else f"Port {port_idx}"}

    def _find_port(self):
        device = self._find_device()
        if not device:
            return None
        for port in device.ports:
            if port.port_idx == self._port_idx:
                return port
        return None

    @property
    def is_on(self) -> bool | None:
        port = self._find_port()
        return port.poe_mode != "off" if port and port.poe_mode is not None else None

    @property
    def available(self) -> bool:
        return super().available and self._find_port() is not None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.client.set_port_poe_mode(self._device_mac, self._port_idx, "auto")
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.client.set_port_poe_mode(self._device_mac, self._port_idx, "off")
        await self.coordinator.async_request_refresh()


class PortEnabledSwitch(_DeviceControlBase):
    """Enable/disable one switch port's forwarding entirely.

    Disabled by default: flipping the wrong port cuts off whatever is
    plugged into it, same reasoning as PoePortSwitch (this is the link-
    level equivalent; a port can have both entities if it supports both).
    """

    _attr_translation_key = "port_enabled_switch"
    _attr_icon = "mdi:ethernet"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: UniFiNetworkPlusCoordinator,
        device_mac: str,
        port_idx: int,
    ) -> None:
        super().__init__(entry, coordinator, device_mac)
        self._port_idx = port_idx
        self._attr_unique_id = f"{entry.entry_id}_{device_mac}_port{port_idx}_enabled_switch"
        port = self._find_port()
        self._attr_translation_placeholders = {"port": port.name if port else f"Port {port_idx}"}

    def _find_port(self):
        device = self._find_device()
        if not device:
            return None
        for port in device.ports:
            if port.port_idx == self._port_idx:
                return port
        return None

    @property
    def is_on(self) -> bool | None:
        port = self._find_port()
        return port.port_enabled if port else None

    @property
    def available(self) -> bool:
        return super().available and self._find_port() is not None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.client.set_port_enabled(self._device_mac, self._port_idx, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.client.set_port_enabled(self._device_mac, self._port_idx, False)
        await self.coordinator.async_request_refresh()


class DeviceLedSwitch(_DeviceControlBase):
    """Turn a device's status LED on or off persistently.

    Distinct from LocateSwitch (a temporary attention-getting blink) -
    this is the permanent "keep the LED dark" preference some people want
    for devices in bedrooms/living spaces. Purely cosmetic, enabled by
    default like Locate.
    """

    _attr_translation_key = "device_led"
    _attr_icon = "mdi:led-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator, device_mac: str) -> None:
        super().__init__(entry, coordinator, device_mac)
        self._attr_unique_id = f"{entry.entry_id}_{device_mac}_led"

    @property
    def is_on(self) -> bool | None:
        device = self._find_device()
        if not device or device.led_override is None:
            return None
        return device.led_override == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.client.set_device_led(self._device_mac, "on")
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.client.set_device_led(self._device_mac, "off")
        await self.coordinator.async_request_refresh()


class BlockClientSwitch(CoordinatorEntity[UniFiNetworkPlusCoordinator], SwitchEntity):
    """Block or unblock a client from the network entirely.

    Unlike LocateSwitch/PoePortSwitch this isn't keyed to a physical UniFi
    device, so it doesn't use _DeviceControlBase - a blocked client has no
    device of its own in this integration (matching device_tracker.py,
    which for the same reason doesn't create one either).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "block_client"
    _attr_icon = "mdi:account-cancel"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator, mac: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._mac = mac
        self._attr_unique_id = f"{entry.entry_id}_{mac}_block"
        client = self._find_client()
        self._attr_translation_placeholders = {"client": client.name if client else mac}

    def _find_client(self):
        if not self.coordinator.data:
            return None
        for client in self.coordinator.data.tracked_clients:
            if client.mac == self._mac:
                return client
        return None

    @property
    def is_on(self) -> bool | None:
        client = self._find_client()
        return client.is_blocked if client else None

    @property
    def _has_control_permission(self) -> bool:
        role = self.coordinator.client.site_role
        return role is None or role == "admin"

    @property
    def available(self) -> bool:
        return super().available and self._find_client() is not None and self._has_control_permission

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.client.set_client_blocked(self._mac, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.client.set_client_blocked(self._mac, False)
        await self.coordinator.async_request_refresh()
