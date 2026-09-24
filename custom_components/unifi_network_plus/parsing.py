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


def _satisfaction(value: Any) -> float | None:
    """Parse a UniFi "satisfaction" percentage.

    Confirmed against live data: the controller uses ``-1`` as a sentinel
    for "no data yet" (e.g. a radio/device with zero connected clients),
    not a real negative percentage - treat it the same as missing.
    """
    num = _num(value)
    return None if num is None or num < 0 else num


@dataclass(frozen=True)
class WanThroughput:
    """Most recent WAN throughput/latency sample."""

    download_mbps: float | None
    upload_mbps: float | None
    latency_ms: float | None
    packet_loss_percent: float | None
    timestamp: int | None


def parse_wan_throughput(
    report_samples: list[dict[str, Any]], bucket_seconds: float = 300
) -> WanThroughput:
    """Parse the latest sample from a stat/report/*.gw response.

    Confirmed against a live UDM-family controller: each
    ``stat/report/5minutes.gw`` sample's ``wan-rx_bytes`` / ``wan-tx_bytes``
    is the total for *that one bucket* (not a running lifetime counter -
    values were observed to vary independently between consecutive
    buckets, and this is also what makes ``parse_monthly_usage`` summing
    daily samples directly do the right thing). Average Mbps for the
    bucket is therefore just bytes-in-bucket * 8 / bucket_seconds.
    ``bucket_seconds`` defaults to 300 (5-minute report); pass a different
    value when parsing the hourly/daily reports instead.

    Latency/loss field names are less consistent across controller
    versions and were not present at all in the samples this was tested
    against (no WAN health-check/speedtest configured on that gateway) -
    we try several known candidates and fall back to None rather than
    guessing wrong; downstream sensors show as unavailable rather than 0
    when that happens.
    """
    if not report_samples:
        return WanThroughput(None, None, None, None, None)

    latest = max(report_samples, key=lambda sample: _num(sample.get("time")) or 0)

    rx_bytes = _num(latest.get("wan-rx_bytes"))
    tx_bytes = _num(latest.get("wan-tx_bytes"))
    download_mbps = (
        round(rx_bytes * 8 / 1_000_000 / bucket_seconds, 2) if rx_bytes is not None else None
    )
    upload_mbps = (
        round(tx_bytes * 8 / 1_000_000 / bucket_seconds, 2) if tx_bytes is not None else None
    )

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
    """Parsed device (AP/switch/gateway) entry from stat/device.

    The extra per-type fields below (temperature/storage for gateways,
    PoE/port-count for switches) are ``None`` when not applicable to that
    device's type - all confirmed against a live controller's actual
    ``stat/device`` payload for one ``uap``, one ``usw`` and one ``uxg``
    device, not guessed from documentation.
    """

    mac: str
    name: str
    model: str | None
    device_type: str | None
    state: int | None
    is_online: bool
    uptime_seconds: int | None
    cpu_percent: float | None
    memory_percent: float | None
    satisfaction_percent: float | None
    client_count: int | None
    cpu_temp_celsius: float | None
    storage_percent: float | None
    poe_power_watts: float | None
    active_ports: int | None
    total_ports: int | None
    radios: list[RadioStat] = field(default_factory=list)


# UniFi device "state" field: 1 = connected/online in the common case.
_ONLINE_STATE = 1


def _parse_cpu_temp(device: dict[str, Any]) -> float | None:
    """Gateways expose a ``temperatures`` list of named sensors; we want
    the one with ``type: "cpu"`` (also seen: "board" for chassis/PHY
    sensors, which we deliberately don't surface as a generic "temperature"
    to avoid ambiguity about which one a plain "Temperature" sensor means).
    """
    temps = device.get("temperatures")
    if not isinstance(temps, list):
        return None
    for t in temps:
        if isinstance(t, dict) and t.get("type") == "cpu":
            return _num(t.get("value"))
    return None


def _parse_storage_percent(device: dict[str, Any]) -> float | None:
    """Gateways expose a ``storage`` list of mount points (e.g. a small
    "/persistent" flash partition used for local backups/logs, separate
    from any attached USB/NVMe storage on models that support it). We
    report the first entry - on the single gateway this was tested
    against there was exactly one ("/persistent").
    """
    storage = device.get("storage")
    if not isinstance(storage, list) or not storage:
        return None
    entry = storage[0]
    if not isinstance(entry, dict):
        return None
    size = _num(entry.get("size"))
    used = _num(entry.get("used"))
    if not size:
        return None
    return round((used or 0) / size * 100, 1)


def _parse_switch_ports(device: dict[str, Any]) -> tuple[float | None, int | None, int | None]:
    """Aggregate a switch's port_table into (poe_power_watts, active_ports,
    total_ports) - intentionally aggregated rather than one sensor per
    port (a 48-port switch would otherwise add 48 near-identical entities).
    """
    ports = device.get("port_table")
    if not isinstance(ports, list) or not ports:
        return None, None, None
    total = len(ports)
    active = sum(1 for p in ports if isinstance(p, dict) and p.get("up"))
    poe_watts: float | None = None
    for p in ports:
        if not isinstance(p, dict):
            continue
        watts = _num(p.get("poe_power"))
        if watts is not None:
            poe_watts = (poe_watts or 0.0) + watts
    return poe_watts, active, total


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
                        satisfaction_percent=_satisfaction(radio.get("satisfaction")),
                    )
                )
        state = _int_or_none(device.get("state"))
        system_stats = device.get("system-stats")
        device_type = device.get("type")

        poe_watts = active_ports = total_ports = None
        if device_type == "usw":
            poe_watts, active_ports, total_ports = _parse_switch_ports(device)

        result.append(
            DeviceInfo(
                mac=str(device.get("mac", "")),
                name=str(device.get("name") or device.get("mac") or "unknown"),
                model=device.get("model"),
                device_type=device_type,
                state=state,
                is_online=state == _ONLINE_STATE,
                uptime_seconds=_int_or_none(device.get("uptime")),
                cpu_percent=_num(system_stats.get("cpu")) if isinstance(system_stats, dict) else None,
                memory_percent=_num(system_stats.get("mem")) if isinstance(system_stats, dict) else None,
                satisfaction_percent=_satisfaction(device.get("satisfaction")),
                client_count=_int_or_none(device.get("num_sta")),
                cpu_temp_celsius=_parse_cpu_temp(device) if device_type == "uxg" else None,
                storage_percent=_parse_storage_percent(device) if device_type == "uxg" else None,
                poe_power_watts=poe_watts,
                active_ports=active_ports,
                total_ports=total_ports,
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


@dataclass(frozen=True)
class WanHealth:
    """WAN-subsystem detail from stat/health - confirmed against a live
    UDM-family controller. Not exposed by ``parse_health`` above (which
    only keeps the small common subset shared by every subsystem type)
    since these fields are WAN-specific.
    """

    isp_name: str | None
    availability_percent: float | None
    latency_ms: float | None
    rx_rate_mbps: float | None
    tx_rate_mbps: float | None


def parse_wan_health(health_entries: list[dict[str, Any]]) -> WanHealth:
    """Extract ISP/latency/availability/live-rate from the "wan" stat/health entry.

    ``uptime_stats.WAN.{latency_average,availability}`` are the controller's
    own rolling WAN-monitor figures (the same ping/DNS probes the UniFi
    Network app's dashboard shows) - a better source for these than the
    ``stat/report`` endpoints, which were not found to carry latency/loss
    fields at all on the controller this was tested against.
    ``{rx,tx}_bytes-r`` are live instantaneous byte/second rates, useful as
    a more responsive throughput reading than the ``stat/report`` buckets
    (which lag by up to one full bucket interval).
    """
    wan = next((e for e in health_entries if e.get("subsystem") == "wan"), None)
    if wan is None:
        return WanHealth(None, None, None, None, None)

    uptime_stats = wan.get("uptime_stats")
    wan_monitor = uptime_stats.get("WAN") if isinstance(uptime_stats, dict) else None

    rx_rate = _num(wan.get("rx_bytes-r"))
    tx_rate = _num(wan.get("tx_bytes-r"))

    return WanHealth(
        isp_name=wan.get("isp_name"),
        availability_percent=_num(wan_monitor.get("availability")) if wan_monitor else None,
        latency_ms=_num(wan_monitor.get("latency_average")) if wan_monitor else None,
        rx_rate_mbps=round(rx_rate * 8 / 1_000_000, 2) if rx_rate is not None else None,
        tx_rate_mbps=round(tx_rate * 8 / 1_000_000, 2) if tx_rate is not None else None,
    )


@dataclass(frozen=True)
class NetworkHealth:
    """Network-wide counts and the controller's own periodic ISP speed
    test, from the "wlan"/"lan"/"www" stat/health entries - confirmed
    against a live UDM-family controller.
    """

    connected_aps: int | None
    switch_count: int | None
    guest_clients: int | None
    iot_clients: int | None
    speedtest_download_mbps: float | None
    speedtest_upload_mbps: float | None
    speedtest_ping_ms: float | None
    speedtest_last_run: int | None
    wan_drops: int | None


def _sum_optional(*values: float | None) -> float | None:
    """Sum values that are present; None only if *none* of them are."""
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def parse_network_health(health_entries: list[dict[str, Any]]) -> NetworkHealth:
    """Extract AP/switch/guest/IoT counts and the ISP speed test result.

    The "www" subsystem's ``xput_down``/``xput_up``/``speedtest_ping`` only
    reflect the controller's *last* speed test (it runs one periodically,
    or on demand via "ISP Speed Test" in the UniFi app) - they read 0 right
    after being reset until the next run completes, not a continuous
    live measurement. ``speedtest_lastrun`` (epoch seconds) tells you how
    stale the reading is.
    """
    wlan = next((e for e in health_entries if e.get("subsystem") == "wlan"), None)
    lan = next((e for e in health_entries if e.get("subsystem") == "lan"), None)
    www = next((e for e in health_entries if e.get("subsystem") == "www"), None)

    return NetworkHealth(
        connected_aps=_int_or_none(wlan.get("num_ap")) if wlan else None,
        switch_count=_int_or_none(lan.get("num_sw")) if lan else None,
        guest_clients=_int_or_none(
            _sum_optional(
                _num(wlan.get("num_guest")) if wlan else None,
                _num(lan.get("num_guest")) if lan else None,
            )
        ),
        iot_clients=_int_or_none(
            _sum_optional(
                _num(wlan.get("num_iot")) if wlan else None,
                _num(lan.get("num_iot")) if lan else None,
            )
        ),
        speedtest_download_mbps=_num(www.get("xput_down")) if www else None,
        speedtest_upload_mbps=_num(www.get("xput_up")) if www else None,
        speedtest_ping_ms=_num(www.get("speedtest_ping")) if www else None,
        speedtest_last_run=_int_or_none(www.get("speedtest_lastrun")) if www else None,
        wan_drops=_int_or_none(www.get("drops")) if www else None,
    )
