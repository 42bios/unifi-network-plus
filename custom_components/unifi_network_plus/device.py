"""Shared device-registry helper for UniFi Network+."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN


def controller_via_device_id(hass: HomeAssistant, entry_id: str) -> str | None:
    """Resolve the shared controller device's registry id.

    Every per-physical-device entity links back to the single controller
    device (identifiers ``(DOMAIN, entry_id)``, created by
    ``UniFiBaseSensor.device_info`` in sensor.py) via ``via_device_id`` -
    which, unlike the deprecated ``via_device`` tuple shortcut, needs the
    target device's actual registry id rather than its identifiers.
    """
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry_id), entry_id)
    return device.id if device else None


def remove_orphaned_client_devices(hass: HomeAssistant, entry_id: str) -> None:
    """Detach this entry from client devices that no longer have an entity of ours.

    Home Assistant keeps a device after its last entity is removed, and the UI
    refuses to delete it. UniFi devices and the controller carry one of our
    identifiers and are never touched; client devices do not.
    """
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, entry_id):
        if any(domain == DOMAIN for domain, _ in device.identifiers):
            continue
        entities = er.async_entries_for_device(entity_registry, device.id, include_disabled_entities=True)
        if any(entity.config_entry_id == entry_id for entity in entities):
            continue
        device_registry.async_update_device(device.id, remove_config_entry_id=entry_id)
