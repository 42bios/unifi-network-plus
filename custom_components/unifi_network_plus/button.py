"""Button (momentary action) entities for UniFi Network+."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RUNTIME_COORDINATOR
from .const import DOMAIN, MANUFACTURER
from .coordinator import UniFiNetworkPlusCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up per-client Reconnect and per-device Restart buttons,
    discovered dynamically."""
    coordinator: UniFiNetworkPlusCoordinator = hass.data[DOMAIN][entry.entry_id][RUNTIME_COORDINATOR]

    known_client_macs: set[str] = set()
    known_device_macs: set[str] = set()

    def _discover_new_entities() -> None:
        if not coordinator.data:
            return
        new_entities = []
        for client in coordinator.data.tracked_clients:
            if client.mac not in known_client_macs:
                known_client_macs.add(client.mac)
                new_entities.append(ReconnectClientButton(entry, coordinator, client.mac))
        for device in coordinator.data.devices:
            if device.mac not in known_device_macs:
                known_device_macs.add(device.mac)
                new_entities.append(RestartDeviceButton(entry, coordinator, device.mac))
        if new_entities:
            async_add_entities(new_entities)

    _discover_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_discover_new_entities))


class ReconnectClientButton(CoordinatorEntity[UniFiNetworkPlusCoordinator], ButtonEntity):
    """Force a connected client to disconnect and immediately re-associate.

    Not a block - just a nudge for a client stuck on a bad AP/band. No
    per-client HA device (see device_tracker.py for why), and disabled by
    default: it's a deliberate, occasional troubleshooting action, not
    something to have ~90 buttons visible for out of the box.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "reconnect_client"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator, mac: str) -> None:
        super().__init__(coordinator)
        self._mac = mac
        self._attr_unique_id = f"{entry.entry_id}_{mac}_reconnect"
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
    def _has_control_permission(self) -> bool:
        role = self.coordinator.client.site_role
        return role is None or role == "admin"

    @property
    def available(self) -> bool:
        return super().available and self._find_client() is not None and self._has_control_permission

    async def async_press(self) -> None:
        await self.coordinator.client.reconnect_client(self._mac)


class RestartDeviceButton(CoordinatorEntity[UniFiNetworkPlusCoordinator], ButtonEntity):
    """Soft-restart a device (AP/switch/gateway).

    Disabled by default: this is a real, disruptive action (the device
    and everything connected through it briefly goes offline), not
    something to have visible-and-tempting for every device out of the
    box. Always a soft restart - see api.py::restart_device.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "restart_device"
    _attr_icon = "mdi:restart"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator, device_mac: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_mac = device_mac
        self._attr_unique_id = f"{entry.entry_id}_{device_mac}_restart"

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
            "via_device": (DOMAIN, self._entry.entry_id),
        }

    @property
    def _has_control_permission(self) -> bool:
        role = self.coordinator.client.site_role
        return role is None or role == "admin"

    @property
    def available(self) -> bool:
        return super().available and self._find_device() is not None and self._has_control_permission

    async def async_press(self) -> None:
        await self.coordinator.client.restart_device(self._device_mac)
