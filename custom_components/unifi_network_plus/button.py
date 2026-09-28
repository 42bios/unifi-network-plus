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
from .device import controller_via_device_id


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up per-client Reconnect, per-device Restart and per-PoE-port
    Power Cycle buttons, discovered dynamically."""
    coordinator: UniFiNetworkPlusCoordinator = hass.data[DOMAIN][entry.entry_id][RUNTIME_COORDINATOR]

    known_client_macs: set[str] = set()
    known_device_macs: set[str] = set()
    known_poe_port_keys: set[tuple[str, int]] = set()

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
            for port in device.ports:
                key = (device.mac, port.port_idx)
                if port.poe_mode is not None and key not in known_poe_port_keys:
                    known_poe_port_keys.add(key)
                    new_entities.append(PowerCyclePortButton(entry, coordinator, device.mac, port.port_idx))
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
            "via_device_id": controller_via_device_id(self.hass, self._entry.entry_id),
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


class PowerCyclePortButton(CoordinatorEntity[UniFiNetworkPlusCoordinator], ButtonEntity):
    """Momentarily power-cycle one PoE port (off then back on).

    Distinct from switch.py's PoePortSwitch, which sets a persistent
    on/off mode - this is a one-shot nudge for a stuck PoE device
    (camera, AP) without leaving it powered off. Disabled by default,
    same reasoning as Restart/Reconnect above.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "power_cycle_port"
    _attr_icon = "mdi:power-cycle"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: UniFiNetworkPlusCoordinator,
        device_mac: str,
        port_idx: int,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_mac = device_mac
        self._port_idx = port_idx
        self._attr_unique_id = f"{entry.entry_id}_{device_mac}_port{port_idx}_power_cycle"
        port = self._find_port()
        self._attr_translation_placeholders = {"port": port.name if port else f"Port {port_idx}"}

    def _find_device(self):
        if not self.coordinator.data:
            return None
        for device in self.coordinator.data.devices:
            if device.mac == self._device_mac:
                return device
        return None

    def _find_port(self):
        device = self._find_device()
        if not device:
            return None
        for port in device.ports:
            if port.port_idx == self._port_idx:
                return port
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
        role = self.coordinator.client.site_role
        return role is None or role == "admin"

    @property
    def available(self) -> bool:
        return super().available and self._find_port() is not None and self._has_control_permission

    async def async_press(self) -> None:
        await self.coordinator.client.power_cycle_port(self._device_mac, self._port_idx)
