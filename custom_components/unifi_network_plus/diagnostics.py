"""Diagnostics support for UniFi Network+."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import format_mac

from . import RUNTIME_COORDINATOR
from .const import DOMAIN

TO_REDACT_CONFIG = {CONF_HOST, CONF_PASSWORD, CONF_USERNAME}
# Fields that directly identify a device, person or network beyond a bare
# MAC (which is separately replaced with an anonymized-but-stable sequence
# below, the same approach the core `unifi` integration's diagnostics use).
TO_REDACT_FIELDS = {"name", "connected_name", "hostname", "ip", "connected_ip", "essid"}


def _collect_macs(value: Any, macs: set[str]) -> None:
    """Walk a nested dict/list structure and collect every MAC-looking string."""
    if isinstance(value, dict):
        for v in value.values():
            _collect_macs(v, macs)
    elif isinstance(value, list):
        for item in value:
            _collect_macs(item, macs)
    elif isinstance(value, str) and value.count(":") == 5:
        macs.add(value)


def _redact(value: Any, mac_map: dict[str, str]) -> Any:
    """Recursively redact PII fields, plus any MAC via the anonymizing map."""
    if isinstance(value, dict):
        return {
            key: REDACTED if key in TO_REDACT_FIELDS else _redact(v, mac_map)
            for key, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, mac_map) for item in value]
    if isinstance(value, str) and value in mac_map:
        return mac_map[value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    coordinator = runtime[RUNTIME_COORDINATOR]
    snapshot = coordinator.data
    snapshot_dict = asdict(snapshot) if snapshot is not None else None

    macs: set[str] = set()
    if snapshot_dict is not None:
        _collect_macs(snapshot_dict, macs)
    # Sorted so the same MAC always gets the same anonymized value across
    # repeated diagnostics downloads for the same controller.
    mac_map = {mac: format_mac(str(i).zfill(12)) for i, mac in enumerate(sorted(macs))}

    return {
        "config_entry": async_redact_data(entry.as_dict(), TO_REDACT_CONFIG),
        "site_role": coordinator.client.site_role,
        "scan_interval_seconds": (
            coordinator.update_interval.total_seconds() if coordinator.update_interval else None
        ),
        "snapshot": _redact(snapshot_dict, mac_map) if snapshot_dict is not None else None,
    }
