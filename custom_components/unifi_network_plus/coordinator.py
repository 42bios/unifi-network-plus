"""DataUpdateCoordinator for UniFi Network+."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import UniFiApiError, UniFiAuthError, UniFiClient
from .const import DEFAULT_TOP_CLIENTS, DOMAIN
from .parsing import (
    DeviceInfo,
    HealthSubsystem,
    MonthlyUsage,
    NetworkHealth,
    TopClient,
    TrackedClient,
    WanLink,
    WlanInfo,
    WanHealth,
    WanThroughput,
    MissingDeviceTracker,
    MonthlyCounterAccumulator,
    merge_wan_fallback,
    parse_devices,
    parse_health,
    parse_monthly_usage,
    parse_network_health,
    parse_top_clients,
    parse_tracked_clients,
    parse_wan_counters,
    parse_wan_health,
    parse_wan_links,
    parse_wlans,
    parse_wan_throughput,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class UniFiSnapshot:
    """Coordinator payload: everything sensors read from."""

    wan: WanThroughput
    wan_health: WanHealth
    network_health: NetworkHealth
    monthly_usage: MonthlyUsage
    top_clients: list[TopClient]
    devices: list[DeviceInfo]
    health: list[HealthSubsystem]
    client_count: int
    tracked_clients: list[TrackedClient]
    wan_links: list[WanLink] = field(default_factory=list)
    wlans: list[WlanInfo] = field(default_factory=list)


class UniFiNetworkPlusCoordinator(DataUpdateCoordinator[UniFiSnapshot]):
    """Poll the UniFi controller for extended network statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        name: str,
        client: UniFiClient,
        scan_interval: int,
        top_clients_count: int = DEFAULT_TOP_CLIENTS,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"UniFi Network+ {name}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.entry_id = entry_id
        self._usage_store: Store = Store(hass, 1, f"{DOMAIN}_{entry_id}_monthly_usage")
        self._usage = MonthlyCounterAccumulator()
        self._usage_saved_at = 0.0
        self._missing_devices = MissingDeviceTracker()
        self._missing_clients = MissingDeviceTracker()
        self.client = client
        self.top_clients_count = top_clients_count

    async def async_load_state(self) -> None:
        """Restore the persisted counter-based monthly usage."""
        self._usage = MonthlyCounterAccumulator.from_dict(await self._usage_store.async_load())

    def _monthly_usage(self, daily_samples: list, raw_devices: list, window_start) -> MonthlyUsage:
        """Report-based total when the controller serves daily reports,
        otherwise accumulated from the gateway's WAN counters (Network 11)."""
        from_reports = parse_monthly_usage(daily_samples, window_start.timestamp() * 1000)
        if from_reports.total_gb is not None:
            return from_reports
        counters = parse_wan_counters(raw_devices)
        if counters is None:
            return from_reports
        now = dt_util.now()
        self._usage.update(now.strftime("%Y-%m"), now.day, *counters)
        # Throttled by hand: async_delay_save restarts its timer on every call,
        # so with a poll interval below the delay it would never fire.
        if time.monotonic() - self._usage_saved_at >= 300:
            self._usage_saved_at = time.monotonic()
            self._usage_store.async_delay_save(self._usage.to_dict, 1)
        return self._usage.usage(now.day)

    def _prune_removed_devices(self, raw_devices: list[dict]) -> None:
        """Drop HA devices (and with them their entities) for UniFi devices
        the controller no longer knows, e.g. after hardware was retired."""
        registry = dr.async_get(self.hass)
        prefix = f"{self.entry_id}_"
        by_mac: dict[str, str] = {}
        for entry in dr.async_entries_for_config_entry(registry, self.entry_id):
            for domain, identifier in entry.identifiers:
                if domain == DOMAIN and identifier.startswith(prefix):
                    by_mac[identifier[len(prefix):]] = entry.id
        seen = {str(d.get("mac")) for d in raw_devices if d.get("mac")}
        for mac in self._missing_devices.update(set(by_mac), seen):
            _LOGGER.info("Removing UniFi device %s: no longer reported by the controller", mac)
            registry.async_update_device(by_mac[mac], remove_config_entry_id=self.entry_id)

    def _prune_forgotten_clients(self, known_clients: list[dict]) -> None:
        """Drop tracker entities for clients the controller has forgotten
        (deleted from its client list), which would otherwise stay
        unavailable forever."""
        registry = er.async_get(self.hass)
        by_mac = {
            entity.unique_id.lower(): entity.entity_id
            for entity in er.async_entries_for_config_entry(registry, self.entry_id)
            if entity.domain == "device_tracker" and entity.unique_id
        }
        seen = {str(c["mac"]).lower() for c in known_clients if c.get("mac")}
        for mac in self._missing_clients.update(set(by_mac), seen):
            _LOGGER.info("Removing tracker for client %s: no longer known to the controller", mac)
            registry.async_remove(by_mac[mac])

    async def _fetch_optional(self, label: str, coro):
        """Run one endpoint fetch; on auth/API error, log and degrade to empty.

        Some controller accounts (e.g. restricted "View Only" roles) may not
        have access to every endpoint we use (the `stat/report/*` endpoints
        in particular have been observed to reject non-admin roles with a
        plain 401 rather than a 403). We don't want one such endpoint to take
        the whole integration into ``setup_retry`` forever - the remaining
        endpoints are still useful on their own.
        """
        try:
            return await coro
        except (UniFiAuthError, UniFiApiError) as err:
            _LOGGER.warning(
                "UniFi Network+: could not fetch %s (%s) - this data will be "
                "unavailable. If this persists, the configured account may "
                "be missing permissions for this endpoint.",
                label,
                err,
            )
            return []

    async def _async_update_data(self) -> UniFiSnapshot:
        # The base client/device/health endpoints are the core of this
        # integration; if even those fail (e.g. real auth failure), surface
        # that clearly instead of silently returning an empty snapshot.
        try:
            clients = await self.client.get_clients()
            devices = await self.client.get_devices()
            health = await self.client.get_health()
        except UniFiAuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except UniFiApiError as err:
            raise UpdateFailed(f"Error communicating with UniFi controller: {err}") from err

        wan_samples = await self._fetch_optional(
            "stat/report/5minutes.gw (WAN throughput)", self.client.get_wan_report_5min()
        )
        daily_samples = await self._fetch_optional(
            "stat/report/daily.gw (monthly usage)", self.client.get_wan_report_daily()
        )
        wlans_raw = await self._fetch_optional("rest/wlanconf (WLANs)", self.client.get_wlans())
        all_known_clients = await self._fetch_optional(
            "rest/user (known clients, for presence tracking)", self.client.get_all_known_clients()
        )

        # Calendar month to date in the local timezone (the daily buckets start
        # at the controller's local midnight). 12 h of tolerance absorbs a
        # controller/HA timezone mismatch without pulling in the previous
        # month's last bucket (always a full 24 h earlier).
        month_start = dt_util.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        window_start = month_start - timedelta(hours=12)

        wan = parse_wan_throughput(wan_samples)
        wan_health = parse_wan_health(health)
        wan = merge_wan_fallback(wan, wan_health)

        self._prune_removed_devices(devices)
        self._prune_forgotten_clients(all_known_clients)

        return UniFiSnapshot(
            wan=wan,
            wan_health=wan_health,
            network_health=parse_network_health(health),
            monthly_usage=self._monthly_usage(daily_samples, devices, window_start),
            top_clients=parse_top_clients(clients, self.top_clients_count),
            devices=_resolve_port_connection_names(parse_devices(devices), clients, devices),
            health=parse_health(health),
            client_count=len(clients),
            tracked_clients=parse_tracked_clients(all_known_clients, clients),
            wan_links=parse_wan_links(devices),
            wlans=parse_wlans(wlans_raw),
        )


def _resolve_port_connection_names(
    devices: list[DeviceInfo],
    raw_clients: list[dict],
    raw_devices: list[dict],
) -> list[DeviceInfo]:
    """Fill in PortStat.connected_name for each port's connected_mac.

    A single port_table entry only carries the MAC/IP of whatever's plugged
    into it (see parsing.py's PortStat) - resolving that to a friendly name
    needs both the client list and the device list (an uplink port often
    connects to another UniFi device, not a regular client), which only the
    coordinator has both of at once.
    """
    mac_to_name: dict[str, str] = {}
    for client in raw_clients:
        mac = client.get("mac")
        if mac:
            mac_to_name[mac] = str(client.get("name") or client.get("hostname") or mac)
    for device in raw_devices:
        mac = device.get("mac")
        if mac:
            mac_to_name[mac] = str(device.get("name") or mac)

    result: list[DeviceInfo] = []
    for device in devices:
        if not device.ports:
            result.append(device)
            continue
        new_ports = [
            replace(port, connected_name=mac_to_name.get(port.connected_mac))
            if port.connected_mac
            else port
            for port in device.ports
        ]
        result.append(replace(device, ports=new_ports))
    return result
