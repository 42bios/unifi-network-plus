"""Shared device-registry helper for UniFi Network+."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

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
