"""DataUpdateCoordinator for UniFi Network+."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
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
    WanHealth,
    WanThroughput,
    parse_devices,
    parse_health,
    parse_monthly_usage,
    parse_top_clients,
    parse_wan_health,
    parse_wan_throughput,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class UniFiSnapshot:
    """Coordinator payload: everything sensors read from."""

    wan: WanThroughput
    wan_health: WanHealth
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

        month_start = datetime.now(timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        # Guard against a controller clock far off from UTC by also
        # looking back a fixed window, whichever is earlier.
        lookback = datetime.now(timezone.utc) - timedelta(days=MONTHLY_USAGE_LOOKBACK_DAYS)
        window_start = min(month_start, lookback)

        wan = parse_wan_throughput(wan_samples)
        wan_health = parse_wan_health(health)
        if wan.latency_ms is None and wan_health.latency_ms is not None:
            # stat/report carries no latency field on some controller
            # versions; stat/health's WAN uptime-monitor average is a
            # reliable fallback where the report data is missing it.
            wan = replace(wan, latency_ms=wan_health.latency_ms)

        return UniFiSnapshot(
            wan=wan,
            wan_health=wan_health,
            monthly_usage=parse_monthly_usage(daily_samples, window_start.timestamp() * 1000),
            top_clients=parse_top_clients(clients, self.top_clients_count),
            devices=parse_devices(devices),
            health=parse_health(health),
            client_count=len(clients),
        )
