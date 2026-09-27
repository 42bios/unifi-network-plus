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
    # Wireless-only (None for wired clients) - confirmed against a live
    # controller's stat/sta response. ``ccq`` ("Client Connectivity
    # Quality") is UniFi's own 0-1000 composite radio-quality score, kept
    # as the raw value rather than re-normalized since its exact formula
    # isn't publicly documented.
    ccq: int | None
    essid: str | None
    channel: int | None
    # Present on both wired and wireless clients - confirmed live.
    network_name: str | None
    vlan: int | None
    # Seconds since this client last associated/connected - confirmed
    # present on both wired and wireless clients.
    uptime_seconds: int | None


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
        is_wired = bool(client.get("is_wired", False))
        parsed.append(
            TopClient(
                name=name,
                mac=str(client.get("mac", "")),
                rx_bytes=rx,
                tx_bytes=tx,
                total_bytes=rx + tx,
                signal_dbm=_num(client.get("signal")),
                is_wired=is_wired,
                ccq=int(ccq) if not is_wired and (ccq := _num(client.get("ccq"))) is not None else None,
                essid=client.get("essid") if not is_wired else None,
                channel=int(ch) if not is_wired and (ch := _num(client.get("channel"))) is not None else None,
                network_name=client.get("network"),
                vlan=_int_or_none(client.get("vlan")),
                uptime_seconds=_int_or_none(client.get("uptime")),
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
class PortStat:
    """One switch port's status, from stat/device's port_table.

    Deliberately more granular than the aggregated ``DeviceInfo.poe_power_watts``/
    ``active_ports`` - callers building per-port entities are expected to
    register them disabled by default (a 48-port switch's worth of per-port
    entities is a lot of clutter for something most users won't look at
    port-by-port day to day), matching how the core ``unifi`` integration's
    own per-port sensors behave.
    """

    port_idx: int
    name: str
    is_up: bool
    speed_mbps: int | None
    poe_power_watts: float | None
    # Live instantaneous rate (not a report bucket average) - same "-r"
    # suffixed field pattern already used for WAN rx/tx, confirmed present
    # per-port too.
    rx_mbps: float | None
    tx_mbps: float | None
    # "GE" (copper Gigabit Ethernet), "SFP+" (fibre/DAC uplink), etc. -
    # confirmed both values present on a live switch's port_table.
    media: str | None
    # "auto"/"off"/"24v"/"passthrough" - only present on PoE-capable ports
    # (confirmed absent on non-PoE ports, matching poe_power_watts).
    poe_mode: str | None
    # Whether the port's forwarding is enabled - inverted from the raw
    # "port_security_enabled" field (see api.py::set_port_enabled for why
    # that name is misleading). Confirmed present on switch ports tested.
    port_enabled: bool | None
    # The network this port carries, e.g. "wan", "wan2", "lan" - confirmed
    # on a UXG-PRO gateway's ports (a switch's ports didn't carry it in
    # the fixtures tested, hence nullable).
    network_name: str | None
    # What's plugged into this port right now, from port_table[].last_connection
    # - confirmed live. mac/ip come straight from that payload; name is
    # resolved separately by the coordinator (which has both the client and
    # device lists to match against) via dataclasses.replace(), not here -
    # a single port dict alone doesn't carry a friendly name for whatever's
    # on the other end of the cable.
    connected_mac: str | None
    connected_ip: str | None
    connected_name: str | None = None


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
    firmware_version: str | None
    firmware_latest_version: str | None
    # -1 is UniFi's "no anomaly data yet" sentinel (same convention as
    # client/satisfaction -1 elsewhere in this API) - kept as None to match.
    anomalies: int | None
    # None when the controller doesn't report this for the device (seen
    # null on an AP in the field, not just absent) rather than always a
    # bool - kept nullable so the entity can go unavailable instead of
    # guessing "not overheating".
    overheating: bool | None
    # Whether the device's locate (blink LED) mode is currently active.
    locating: bool | None
    # "on"/"off"/"default" (follows the site-wide LED setting) - confirmed
    # live as "off" on an AP.
    led_override: str | None
    radios: list[RadioStat] = field(default_factory=list)
    ports: list[PortStat] = field(default_factory=list)


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


def _parse_ports(device: dict[str, Any]) -> list[PortStat]:
    """Per-port detail for a switch's port_table (see PortStat)."""
    ports = device.get("port_table")
    if not isinstance(ports, list):
        return []
    result: list[PortStat] = []
    for p in ports:
        if not isinstance(p, dict):
            continue
        idx = _int_or_none(p.get("port_idx"))
        if idx is None:
            continue
        rx_rate = _num(p.get("rx_bytes-r"))
        tx_rate = _num(p.get("tx_bytes-r"))
        last_connection = p.get("last_connection")
        if not isinstance(last_connection, dict) or not last_connection.get("connected"):
            last_connection = None
        result.append(
            PortStat(
                port_idx=idx,
                name=str(p.get("name") or f"Port {idx}"),
                is_up=bool(p.get("up")),
                speed_mbps=_int_or_none(p.get("speed")),
                poe_power_watts=_num(p.get("poe_power")),
                rx_mbps=round(rx_rate * 8 / 1_000_000, 3) if rx_rate is not None else None,
                tx_mbps=round(tx_rate * 8 / 1_000_000, 3) if tx_rate is not None else None,
                media=p.get("media"),
                poe_mode=p.get("poe_mode"),
                port_enabled=(not p["port_security_enabled"]) if "port_security_enabled" in p else None,
                network_name=p.get("network_name"),
                connected_mac=last_connection.get("mac") if last_connection else None,
                connected_ip=last_connection.get("ip") if last_connection else None,
            )
        )
    return result


def _parse_firmware(device: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (installed_version, latest_version).

    Confirmed against live data: ``upgradable`` (bool) and
    ``upgrade_to_firmware`` (the target version string, only populated
    when an update is actually available) - none of the 10 devices tested
    against currently had a pending update, so ``upgrade_to_firmware`` was
    only observed as ``None``; falling back to the installed version when
    it's missing even though ``upgradable`` is set keeps this from ever
    reporting a nonsensical "update to None".
    """
    installed = device.get("displayable_version") or device.get("version")
    installed = str(installed) if installed else None
    latest = installed
    if device.get("upgradable") and device.get("upgrade_to_firmware"):
        latest = str(device["upgrade_to_firmware"])
    return installed, latest


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
        # Per-port entities apply to any device with a port_table, not just
        # switches - confirmed live: a UXG-PRO gateway's WAN/WAN2/LAN/SFP+
        # ports carry the same port_table shape (media, rx/tx-r, etc.) as a
        # switch's. Only the aggregated PoE Power/Active Ports metrics
        # above stay switch-only (a gateway's ports aren't PoE sources).
        ports = _parse_ports(device) if isinstance(device.get("port_table"), list) else []

        firmware_version, firmware_latest_version = _parse_firmware(device)

        anomalies = _int_or_none(device.get("anomalies"))
        if anomalies is not None and anomalies < 0:
            anomalies = None  # -1 sentinel: no anomaly data yet
        overheating_raw = device.get("overheating")

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
                firmware_version=firmware_version,
                firmware_latest_version=firmware_latest_version,
                anomalies=anomalies,
                overheating=bool(overheating_raw) if overheating_raw is not None else None,
                locating=bool(device.get("locating")) if device.get("locating") is not None else None,
                led_override=device.get("led_override"),
                radios=radios,
                ports=ports,
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
    # Secondary WAN (failover) availability - confirmed present in
    # uptime_stats as its own "WAN2" entry on a live controller even
    # without a second WAN actually configured (reporting 0% there), so
    # None here specifically means "no WAN2 entry at all", not "down".
    wan2_availability_percent: float | None


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
        return WanHealth(None, None, None, None, None, None)

    uptime_stats = wan.get("uptime_stats")
    wan_monitor = uptime_stats.get("WAN") if isinstance(uptime_stats, dict) else None
    wan2_monitor = uptime_stats.get("WAN2") if isinstance(uptime_stats, dict) else None

    rx_rate = _num(wan.get("rx_bytes-r"))
    tx_rate = _num(wan.get("tx_bytes-r"))

    return WanHealth(
        isp_name=wan.get("isp_name"),
        availability_percent=_num(wan_monitor.get("availability")) if wan_monitor else None,
        latency_ms=_num(wan_monitor.get("latency_average")) if wan_monitor else None,
        rx_rate_mbps=round(rx_rate * 8 / 1_000_000, 2) if rx_rate is not None else None,
        tx_rate_mbps=round(tx_rate * 8 / 1_000_000, 2) if tx_rate is not None else None,
        wan2_availability_percent=_num(wan2_monitor.get("availability")) if wan2_monitor else None,
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


@dataclass(frozen=True)
class TrackedClient:
    """One known client for presence tracking (``device_tracker``).

    Built from ``rest/user`` (every client the controller has ever seen,
    confirmed live: 90 entries vs. a handful currently connected) merged
    with ``stat/sta`` (who's online *right now*) - ``rest/user`` alone has
    no "currently connected" flag, and ``stat/sta`` alone simply omits a
    client once it disconnects rather than reporting it offline, so
    neither endpoint alone is enough for a "not_home" state.
    """

    mac: str
    name: str
    is_online: bool
    is_wired: bool
    is_guest: bool
    is_blocked: bool
    ip: str | None
    last_seen: int | None
    network_name: str | None
    essid: str | None
    manufacturer: str | None


def parse_tracked_clients(
    all_known_clients: list[dict[str, Any]],
    online_clients: list[dict[str, Any]],
) -> list[TrackedClient]:
    """Merge the full known-client roster with the currently-connected list."""
    online_by_mac = {c["mac"]: c for c in online_clients if c.get("mac")}
    result: list[TrackedClient] = []
    for entry in all_known_clients:
        mac = entry.get("mac")
        if not mac:
            continue
        online_entry = online_by_mac.get(mac)
        is_online = online_entry is not None
        # Prefer the live stat/sta entry's ip/network for an online client
        # (more current than rest/user's "last" fields), fall back to
        # rest/user's last-known values for an offline one.
        source = online_entry if online_entry else entry
        name = str(
            entry.get("name")
            or entry.get("hostname")
            or entry.get("device_name")
            or mac
        )
        result.append(
            TrackedClient(
                mac=mac,
                name=name,
                is_online=is_online,
                is_wired=bool(source.get("is_wired", False)),
                is_guest=bool(entry.get("is_guest", False)),
                is_blocked=bool(entry.get("blocked", False)),
                ip=source.get("ip") or source.get("last_ip"),
                last_seen=_int_or_none(entry.get("last_seen")),
                network_name=source.get("network") or source.get("last_connection_network_name"),
                essid=online_entry.get("essid") if online_entry and not source.get("is_wired") else None,
                manufacturer=entry.get("oui") or None,
            )
        )
    return result
