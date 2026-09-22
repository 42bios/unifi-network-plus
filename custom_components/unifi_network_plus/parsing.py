"""Pure functions that turn raw UniFi controller JSON into typed data.

Kept separate from api.py/coordinator.py so it can be unit tested with
plain fixture dictionaries, without any networking or Home Assistant
involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _num(value: Any) -> float | None:
    """Coerce a value to float, returning None for missing/invalid data."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class WanThroughput:
    """Most recent WAN throughput/latency sample."""

    download_mbps: float | None
    upload_mbps: float | None
    latency_ms: float | None
    packet_loss_percent: float | None
    timestamp: int | None


def parse_wan_throughput(report_samples: list[dict[str, Any]]) -> WanThroughput:
    """Parse the latest sample from a stat/report/*.gw response.

    UniFi report samples carry cumulative and (on most versions) rate
    fields. Rate fields commonly seen across controller versions:
    ``wan-rx_bytes-r`` / ``wan-tx_bytes-r`` (bytes/second at sample time).
    Latency/loss field names vary more between versions
    (``wan-latency_avg``, ``latency``, ...); we try several known
    candidates and fall back to None rather than guessing wrong.
    """
    if not report_samples:
        return WanThroughput(None, None, None, None, None)

    latest = max(
        report_samples,
        key=lambda sample: _num(sample.get("time")) or 0,
    )

    rx_rate = _num(latest.get("wan-rx_bytes-r"))
    tx_rate = _num(latest.get("wan-tx_bytes-r"))
    download_mbps = round(rx_rate * 8 / 1_000_000, 2) if rx_rate is not None else None
    upload_mbps = round(tx_rate * 8 / 1_000_000, 2) if tx_rate is not None else None

    latency_ms = None
    for key in ("wan-latency_avg", "latency", "wan-latency"):
        latency_ms = _num(latest.get(key))
        if latency_ms is not None:
            break

    packet_loss = None
    for key in ("wan-packet_loss", "packet_loss", "loss"):
        packet_loss = _num(latest.get(key))
        if packet_loss is not None:
            break

    return WanThroughput(
        download_mbps=download_mbps,
        upload_mbps=upload_mbps,
        latency_ms=latency_ms,
        packet_loss_percent=packet_loss,
        timestamp=int(latest["time"]) if isinstance(latest.get("time"), (int, float)) else None,
    )


@dataclass(frozen=True)
class MonthlyUsage:
    """Aggregated monthly WAN data usage."""

    download_gb: float | None
    upload_gb: float | None
    total_gb: float | None
    days_counted: int


def parse_monthly_usage(daily_samples: list[dict[str, Any]], month_epoch_start_ms: float) -> MonthlyUsage:
    """Sum daily WAN rx/tx bytes for samples within the current calendar month."""
    rx_total = 0.0
    tx_total = 0.0
    counted = 0
    have_any = False

    for sample in daily_samples:
        ts = _num(sample.get("time"))
        if ts is None or ts < month_epoch_start_ms:
            continue
        rx = _num(sample.get("wan-rx_bytes"))
        tx = _num(sample.get("wan-tx_bytes"))
        if rx is None and tx is None:
            continue
        have_any = True
        rx_total += rx or 0.0
        tx_total += tx or 0.0
        counted += 1

    if not have_any:
        return MonthlyUsage(None, None, None, 0)

    download_gb = round(rx_total / 1_000_000_000, 2)
    upload_gb = round(tx_total / 1_000_000_000, 2)
    return MonthlyUsage(download_gb, upload_gb, round(download_gb + upload_gb, 2), counted)


@dataclass(frozen=True)
class TopClient:
    """One entry of the top-clients-by-traffic list."""

    name: str
    mac: str
    rx_bytes: float
    tx_bytes: float
    total_bytes: float
    signal_dbm: float | None
    is_wired: bool


def parse_top_clients(clients: list[dict[str, Any]], count: int) -> list[TopClient]:
    """Rank connected clients (stat/sta) by total traffic volume."""
    parsed: list[TopClient] = []
    for client in clients:
        rx = _num(client.get("rx_bytes")) or 0.0
        tx = _num(client.get("tx_bytes")) or 0.0
        name = str(
            client.get("name")
            or client.get("hostname")
            or client.get("mac")
            or "unknown"
        )
        parsed.append(
            TopClient(
                name=name,
                mac=str(client.get("mac", "")),
                rx_bytes=rx,
                tx_bytes=tx,
                total_bytes=rx + tx,
                signal_dbm=_num(client.get("signal")),
                is_wired=bool(client.get("is_wired", False)),
            )
        )
    parsed.sort(key=lambda c: c.total_bytes, reverse=True)
    return parsed[: max(count, 0)]


@dataclass(frozen=True)
class RadioStat:
    """Per-radio statistics for one AP (from stat/device radio_table_stats)."""

    radio: str
    channel: int | None
    channel_utilization_percent: float | None
    tx_retries_percent: float | None
    num_clients: int | None
    satisfaction_percent: float | None


@dataclass(frozen=True)
class DeviceInfo:
    """Parsed device (AP/switch/gateway) entry from stat/device."""

    mac: str
    name: str
    model: str | None
    device_type: str | None
    state: int | None
    is_online: bool
    uptime_seconds: int | None
    radios: list[RadioStat] = field(default_factory=list)


# UniFi device "state" field: 1 = connected/online in the common case.
_ONLINE_STATE = 1


def parse_devices(devices: list[dict[str, Any]]) -> list[DeviceInfo]:
    """Parse stat/device entries, including per-radio channel/retry stats."""
    result: list[DeviceInfo] = []
    for device in devices:
        radios: list[RadioStat] = []
        radio_stats = device.get("radio_table_stats")
        if isinstance(radio_stats, list):
            for radio in radio_stats:
                if not isinstance(radio, dict):
                    continue
                radios.append(
                    RadioStat(
                        radio=str(radio.get("name") or radio.get("radio") or "radio"),
                        channel=_int_or_none(radio.get("channel")),
                        channel_utilization_percent=_num(radio.get("cu_total")),
                        tx_retries_percent=_num(radio.get("tx_retries")),
                        num_clients=_int_or_none(radio.get("num_sta")),
                        satisfaction_percent=_num(radio.get("satisfaction")),
                    )
                )
        state = _int_or_none(device.get("state"))
        result.append(
            DeviceInfo(
                mac=str(device.get("mac", "")),
                name=str(device.get("name") or device.get("mac") or "unknown"),
                model=device.get("model"),
                device_type=device.get("type"),
                state=state,
                is_online=state == _ONLINE_STATE,
                uptime_seconds=_int_or_none(device.get("uptime")),
                radios=radios,
            )
        )
    return result


def _int_or_none(value: Any) -> int | None:
    num = _num(value)
    return int(num) if num is not None else None


@dataclass(frozen=True)
class HealthSubsystem:
    """One entry from stat/health."""

    subsystem: str
    status: str | None
    wan_ip: str | None
    gw_mac: str | None
    num_user: int | None


def parse_health(health_entries: list[dict[str, Any]]) -> list[HealthSubsystem]:
    """Parse stat/health subsystem status entries."""
    result: list[HealthSubsystem] = []
    for entry in health_entries:
        result.append(
            HealthSubsystem(
                subsystem=str(entry.get("subsystem", "unknown")),
                status=entry.get("status"),
                wan_ip=entry.get("wan_ip"),
                gw_mac=entry.get("gw_mac"),
                num_user=_int_or_none(entry.get("num_user")),
            )
        )
    return result
