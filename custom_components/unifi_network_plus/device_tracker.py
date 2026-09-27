"""Client presence tracking for UniFi Network+.

Built from ``coordinator.data.tracked_clients`` (see parsing.py's
``TrackedClient``/``parse_tracked_clients`` for how "known but offline" is
distinguished from "currently connected" - the two source endpoints,
rest/user and stat/sta, each only cover half of that on their own).

``ScannerEntity`` (not ``TrackerEntity``) is the right base here: it's for
devices tracked by *connection state* to a router/AP, not GPS coordinates,
and is what core Home Assistant's own "unifi" integration uses for the
same purpose. Two of its behaviors are easy to miss and matter here:
``unique_id`` is forced to always equal ``mac_address`` (not a free
choice), and ``device_info`` is forced to ``None`` - the base class
deliberately does not create a device registry entry per tracked client.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import ScannerEntity, SourceType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RUNTIME_COORDINATOR
from .const import DOMAIN
from .coordinator import UniFiNetworkPlusCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up client presence trackers, discovered dynamically as the
    controller reports new known clients (same pattern as sensor.py).
    """
    coordinator: UniFiNetworkPlusCoordinator = hass.data[DOMAIN][entry.entry_id][RUNTIME_COORDINATOR]

    known_macs: set[str] = set()

    def _discover_new_clients() -> None:
        if not coordinator.data:
            return
        new_entities = []
        for client in coordinator.data.tracked_clients:
            if client.mac not in known_macs:
                known_macs.add(client.mac)
                new_entities.append(UniFiClientTracker(coordinator, client.mac))
        if new_entities:
            async_add_entities(new_entities)

    _discover_new_clients()
    entry.async_on_unload(coordinator.async_add_listener(_discover_new_clients))


class UniFiClientTracker(CoordinatorEntity[UniFiNetworkPlusCoordinator], ScannerEntity):
    """One known client, tracked by its current connection state."""

    # Deliberately NOT has_entity_name=True: device_info is forced to None
    # (see module docstring), so there's no device name to compose with -
    # `name` below is used as the entity's whole display name.
    _attr_source_type = SourceType.ROUTER

    def __init__(self, coordinator: UniFiNetworkPlusCoordinator, mac: str) -> None:
        super().__init__(coordinator)
        self._mac = mac
        self._attr_mac_address = mac

    def _find_client(self):
        if not self.coordinator.data:
            return None
        for client in self.coordinator.data.tracked_clients:
            if client.mac == self._mac:
                return client
        return None

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Always enabled - ScannerEntity's own default (disabled unless
        some *other* integration already claims this MAC) is a property,
        not backed by _attr_entity_registry_enabled_default, so it has to
        be overridden here rather than via that class attribute.
        """
        return True

    @property
    def name(self) -> str:
        client = self._find_client()
        return client.name if client else self._mac

    @property
    def is_connected(self) -> bool | None:
        client = self._find_client()
        return client.is_online if client else None

    @property
    def ip_address(self) -> str | None:
        client = self._find_client()
        return client.ip if client else None

    @property
    def hostname(self) -> str | None:
        client = self._find_client()
        return client.name if client else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        client = self._find_client()
        if not client:
            return {}
        return {
            "is_wired": client.is_wired,
            "is_guest": client.is_guest,
            "is_blocked": client.is_blocked,
            "last_seen": client.last_seen,
            "network": client.network_name,
            "essid": client.essid,
            "manufacturer": client.manufacturer,
        }

    @property
    def available(self) -> bool:
        return super().available and self._find_client() is not None
