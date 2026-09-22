"""DataUpdateCoordinator for UniFi Network+."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import UniFiApiError, UniFiAuthError, UniFiClient
from .const import DEFAULT_TOP_CLIENTS, MONTHLY_USAGE_LOOKBACK_DAYS
from .parsing import (
    DeviceInfo,
    HealthSubsystem,
    MonthlyUsage,
    TopClient,
    WanThroughput,
    parse_devices,
    parse_health,
    parse_monthly_usage,
    parse_top_clients,
    parse_wan_throughput,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class UniFiSnapshot:
    """Coordinator payload: everything sensors read from."""

    wan: WanThroughput
    monthly_usage: MonthlyUsage
    top_clients: list[TopClient]
    devices: list[DeviceInfo]
    health: list[HealthSubsystem]
    client_count: int


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

    async def _async_update_data(self) -> UniFiSnapshot:
        try:
            wan_samples = await self.client.get_wan_report_5min()
            clients = await self.client.get_clients()
            devices = await self.client.get_devices()
            health = await self.client.get_health()
            daily_samples = await self.client.get_wan_report_daily()
        except UniFiAuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except UniFiApiError as err:
            raise UpdateFailed(f"Error communicating with UniFi controller: {err}") from err

        month_start = datetime.now(timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        # Guard against a controller clock far off from UTC by also
        # looking back a fixed window, whichever is earlier.
        lookback = datetime.now(timezone.utc) - timedelta(days=MONTHLY_USAGE_LOOKBACK_DAYS)
        window_start = min(month_start, lookback)

        return UniFiSnapshot(
            wan=parse_wan_throughput(wan_samples),
            monthly_usage=parse_monthly_usage(daily_samples, window_start.timestamp() * 1000),
            top_clients=parse_top_clients(clients, self.top_clients_count),
            devices=parse_devices(devices),
            health=parse_health(health),
            client_count=len(clients),
        )
