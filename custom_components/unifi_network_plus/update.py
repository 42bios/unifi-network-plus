"""Firmware update entities for UniFi Network+.

One per physical UniFi device (AP/switch/gateway), discovered dynamically
the same way sensor.py discovers per-device sensors - a newly adopted
device gets an update entity automatically on the next poll.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateEntity
from homeassistant.config_entries import ConfigEntry
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
    """Set up one firmware Update entity per UniFi device."""
    coordinator: UniFiNetworkPlusCoordinator = hass.data[DOMAIN][entry.entry_id][RUNTIME_COORDINATOR]

    known_device_macs: set[str] = set()

    def _discover_new_devices() -> None:
        if not coordinator.data:
            return
        new_entities = []
        for device in coordinator.data.devices:
            if device.mac in known_device_macs:
                continue
            known_device_macs.add(device.mac)
            new_entities.append(UniFiDeviceUpdateEntity(entry, coordinator, device.mac))
        if new_entities:
            async_add_entities(new_entities)

    _discover_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_discover_new_devices))


class UniFiDeviceUpdateEntity(CoordinatorEntity[UniFiNetworkPlusCoordinator], UpdateEntity):
    """Firmware update status for one UniFi device.

    Read-only: this does not implement ``async_install`` (triggering a
    firmware upgrade over the API is possible on UniFi controllers, but
    remotely flashing network infrastructure firmware from Home Assistant
    without a very deliberate, separate opt-in is more risk than this
    integration wants to take on by default - see README).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "firmware"

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: UniFiNetworkPlusCoordinator,
        device_mac: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_mac = device_mac
        self._attr_unique_id = f"{entry.entry_id}_{device_mac}_firmware"

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
    def installed_version(self) -> str | None:
        device = self._find_device()
        return device.firmware_version if device else None

    @property
    def latest_version(self) -> str | None:
        device = self._find_device()
        return device.firmware_latest_version if device else None

    @property
    def available(self) -> bool:
        return super().available and self._find_device() is not None
