"""Binary sensors for UniFi Network+."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RUNTIME_COORDINATOR, RUNTIME_EVENT_LISTENER
from .const import DOMAIN, MANUFACTURER
from .coordinator import UniFiNetworkPlusCoordinator
from .websocket import UniFiEventListener


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the real-time-connection sensor plus per-device diagnostic
    binary sensors (discovered dynamically, same pattern as sensor.py).
    """
    listener: UniFiEventListener = hass.data[DOMAIN][entry.entry_id][RUNTIME_EVENT_LISTENER]
    coordinator: UniFiNetworkPlusCoordinator = hass.data[DOMAIN][entry.entry_id][RUNTIME_COORDINATOR]
    async_add_entities([RealtimeConnectedSensor(entry, listener)])

    known_device_macs: set[str] = set()

    def _discover_new_devices() -> None:
        if not coordinator.data:
            return
        new_entities: list[BinarySensorEntity] = []
        for device in coordinator.data.devices:
            if device.mac not in known_device_macs:
                known_device_macs.add(device.mac)
                new_entities.append(OverheatingSensor(entry, coordinator, device.mac))
        if new_entities:
            async_add_entities(new_entities)

    _discover_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_discover_new_devices))


class RealtimeConnectedSensor(BinarySensorEntity):
    """Whether the WebSocket event stream is currently connected.

    Purely diagnostic: the integration works the same either way (see
    websocket.py) - this just tells you whether it's currently getting the
    faster, event-triggered refreshes or has fallen back to its plain
    polling schedule (e.g. because the controller/firmware doesn't support
    the events endpoint, or the connection dropped and hasn't reconnected
    yet). Polls the listener's in-memory flag every 10s rather than pushing
    on every connect/disconnect - good enough for a status indicator
    without adding another event-driven code path.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "realtime_connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, listener: UniFiEventListener) -> None:
        self._entry = entry
        self._listener = listener
        self._attr_unique_id = f"{entry.entry_id}_realtime_connected"
        self._unsub = None

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": self._entry.title,
            "manufacturer": MANUFACTURER,
            "model": "Network Controller",
        }

    @property
    def is_on(self) -> bool:
        return self._listener.connected

    async def async_added_to_hass(self) -> None:
        self._unsub = async_track_time_interval(self.hass, self._refresh, timedelta(seconds=10))

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()

    @callback
    def _refresh(self, _now) -> None:
        self.async_write_ha_state()


class OverheatingSensor(CoordinatorEntity[UniFiNetworkPlusCoordinator], BinarySensorEntity):
    """Whether a physical UniFi device is reporting an overheating condition.

    ``overheating`` was confirmed present in a live device's stat/device
    payload but was ``null`` (not ``false``) on every device tested against
    - so this goes unavailable rather than assuming "not overheating" when
    the controller hasn't reported a value at all.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "overheating"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator, device_mac: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_mac = device_mac
        self._attr_unique_id = f"{entry.entry_id}_{device_mac}_overheating"

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
    def is_on(self) -> bool | None:
        device = self._find_device()
        return device.overheating if device else None

    @property
    def available(self) -> bool:
        device = self._find_device()
        return device is not None and device.overheating is not None
