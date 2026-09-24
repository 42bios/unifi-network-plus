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

from . import RUNTIME_EVENT_LISTENER
from .const import DOMAIN, MANUFACTURER
from .websocket import UniFiEventListener


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the real-time-connection diagnostic sensor."""
    listener: UniFiEventListener = hass.data[DOMAIN][entry.entry_id][RUNTIME_EVENT_LISTENER]
    async_add_entities([RealtimeConnectedSensor(entry, listener)])


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
