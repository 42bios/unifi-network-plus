"""DataUpdateCoordinator for UniFi Network+."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import UniFiApiError, UniFiAuthError, UniFiClient
from .const import DEFAULT_TOP_CLIENTS
from .parsing import (
    DeviceInfo,
    HealthSubsystem,
    MonthlyUsage,
    NetworkHealth,
    TopClient,
    TrackedClient,
    WanHealth,
    WanThroughput,
    merge_wan_fallback,
    parse_devices,
    parse_health,
    parse_monthly_usage,
    parse_network_health,
    parse_top_clients,
    parse_tracked_clients,
    parse_wan_health,
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
        self.client = client
        self.top_clients_count = top_clients_count

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

        return UniFiSnapshot(
            wan=wan,
            wan_health=wan_health,
            network_health=parse_network_health(health),
            monthly_usage=parse_monthly_usage(daily_samples, window_start.timestamp() * 1000),
            top_clients=parse_top_clients(clients, self.top_clients_count),
            devices=_resolve_port_connection_names(parse_devices(devices), clients, devices),
            health=parse_health(health),
            client_count=len(clients),
            tracked_clients=parse_tracked_clients(all_known_clients, clients),
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
