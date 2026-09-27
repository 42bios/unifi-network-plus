"""Button (momentary action) entities for UniFi Network+."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
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
    """Set up per-client Reconnect buttons, discovered dynamically."""
    coordinator: UniFiNetworkPlusCoordinator = hass.data[DOMAIN][entry.entry_id][RUNTIME_COORDINATOR]

    known_client_macs: set[str] = set()

    def _discover_new_clients() -> None:
        if not coordinator.data:
            return
        new_entities = []
        for client in coordinator.data.tracked_clients:
            if client.mac not in known_client_macs:
                known_client_macs.add(client.mac)
                new_entities.append(ReconnectClientButton(entry, coordinator, client.mac))
        if new_entities:
            async_add_entities(new_entities)

    _discover_new_clients()
    entry.async_on_unload(coordinator.async_add_listener(_discover_new_clients))


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
