"""Unit tests for parsing.py using fixture-style raw UniFi JSON payloads.

These do not require networking or Home Assistant - they exercise the pure
parsing functions directly against representative response shapes (the
``stat/health`` "wan" subsystem fixtures below are trimmed from an actual
live UDM-family controller response, not guessed).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "unifi_network_plus"))

from parsing import (  # noqa: E402
    parse_devices,
    parse_health,
    parse_monthly_usage,
    parse_network_health,
    parse_top_clients,
    parse_wan_health,
    parse_wan_throughput,
)


def test_parse_wan_throughput_empty() -> None:
    result = parse_wan_throughput([])
    assert result.download_mbps is None
    assert result.upload_mbps is None
    assert result.latency_ms is None
    assert result.packet_loss_percent is None


def test_parse_wan_throughput_picks_latest_and_converts_units() -> None:
    # UniFi report samples carry the byte total *for that one bucket*, not a
    # running lifetime counter (confirmed against a live controller - values
    # were observed to vary independently between consecutive buckets).
    samples = [
        {
            "time": 0,
            "wan-rx_bytes": 300_000_000,
            "wan-tx_bytes": 37_500_000,
            "wan-latency_avg": 12.5,
        },
        {
            "time": 300_000,  # later bucket wins
            "wan-rx_bytes": 600_000_000,  # * 8 / 1e6 / 300s -> 16 Mbps
            "wan-tx_bytes": 75_000_000,  # * 8 / 1e6 / 300s -> 2 Mbps
            "wan-latency_avg": 8.0,
        },
    ]
    result = parse_wan_throughput(samples)
    assert result.timestamp == 300_000
    assert result.download_mbps == 16.0
    assert result.upload_mbps == 2.0
    assert result.latency_ms == 8.0


def test_parse_wan_throughput_respects_bucket_seconds() -> None:
    # An hourly-report sample covers a 3600s bucket, not 300s.
    samples = [{"time": 0, "wan-rx_bytes": 3_600_000_000, "wan-tx_bytes": 0}]
    result = parse_wan_throughput(samples, bucket_seconds=3600)
    # 3_600_000_000 * 8 / 1e6 / 3600 = 8 Mbps
    assert result.download_mbps == 8.0


def test_parse_wan_throughput_missing_fields_returns_none_not_crash() -> None:
    samples = [{"time": 1000}]
    result = parse_wan_throughput(samples)
    assert result.download_mbps is None
    assert result.upload_mbps is None
    assert result.latency_ms is None
    assert result.packet_loss_percent is None


def test_parse_monthly_usage_filters_by_window_and_sums() -> None:
    samples = [
        {"time": 500, "wan-rx_bytes": 1_000_000_000, "wan-tx_bytes": 500_000_000},  # before window
        {"time": 2_000, "wan-rx_bytes": 2_000_000_000, "wan-tx_bytes": 1_000_000_000},
        {"time": 3_000, "wan-rx_bytes": 1_000_000_000, "wan-tx_bytes": 0},
    ]
    result = parse_monthly_usage(samples, month_epoch_start_ms=1_000)
    assert result.days_counted == 2
    assert result.download_gb == 3.0
    assert result.upload_gb == 1.0
    assert result.total_gb == 4.0


def test_parse_monthly_usage_no_data() -> None:
    result = parse_monthly_usage([], month_epoch_start_ms=0)
    assert result.total_gb is None
    assert result.days_counted == 0


def test_parse_top_clients_ranks_by_total_traffic() -> None:
    clients = [
        {"name": "laptop", "mac": "aa:bb", "rx_bytes": 100, "tx_bytes": 50, "signal": -60},
        {"name": "phone", "mac": "cc:dd", "rx_bytes": 5000, "tx_bytes": 5000, "is_wired": False},
        {"hostname": "nas", "mac": "ee:ff", "rx_bytes": 900000, "tx_bytes": 100000, "is_wired": True},
    ]
    top = parse_top_clients(clients, count=2)
    assert len(top) == 2
    assert top[0].name == "nas"
    assert top[0].is_wired is True
    assert top[1].name == "phone"


def test_parse_top_clients_count_zero() -> None:
    clients = [{"name": "a", "mac": "1", "rx_bytes": 1, "tx_bytes": 1}]
    assert parse_top_clients(clients, count=0) == []


def test_parse_top_clients_missing_name_falls_back_to_mac() -> None:
    clients = [{"mac": "11:22:33", "rx_bytes": 10, "tx_bytes": 10}]
    top = parse_top_clients(clients, count=5)
    assert top[0].name == "11:22:33"


def test_parse_top_clients_wireless_extras_from_live_fixture() -> None:
    # Trimmed from a real wireless stat/sta entry on a live UDM-family
    # controller - not a guess.
    client = {
        "name": "Bambu Lab H2D",
        "mac": "aa:bb:cc:dd:ee:10",
        "rx_bytes": 12368140584,
        "tx_bytes": 626868121,
        "is_wired": False,
        "signal": -56,
        "ccq": 333,
        "essid": "MyHomeWiFi",
        "channel": 36,
        "network": "CLIENTS",
        "vlan": 30,
    }
    top = parse_top_clients([client], count=1)
    assert top[0].ccq == 333
    assert top[0].essid == "MyHomeWiFi"
    assert top[0].channel == 36
    assert top[0].network_name == "CLIENTS"
    assert top[0].vlan == 30


def test_parse_top_clients_wired_extras_are_none() -> None:
    client = {
        "name": "doorbell",
        "mac": "aa:bb:cc:dd:ee:11",
        "rx_bytes": 245178308082,
        "tx_bytes": 17123691735,
        "is_wired": True,
        # A wired client's payload has no ccq/essid/channel fields at all;
        # simulate that omission rather than asserting on values that
        # wouldn't be there.
    }
    top = parse_top_clients([client], count=1)
    assert top[0].ccq is None
    assert top[0].essid is None
    assert top[0].channel is None


def test_parse_devices_with_radio_table_stats() -> None:
    devices = [
        {
            "mac": "aa:bb:cc",
            "name": "Living Room AP",
            "model": "U6-Pro",
            "type": "uap",
            "state": 1,
            "uptime": 12345,
            "radio_table_stats": [
                {"name": "wifi0", "channel": 36, "cu_total": 22, "tx_retries": 3.5, "num_sta": 5, "satisfaction": 98},
                {"name": "wifi1", "channel": 6, "cu_total": 55, "tx_retries": 12.0, "num_sta": 8},
            ],
        },
        {"mac": "dd:ee:ff", "name": "Offline Switch", "state": 0},
    ]
    parsed = parse_devices(devices)
    assert len(parsed) == 2

    ap = parsed[0]
    assert ap.is_online is True
    assert len(ap.radios) == 2
    assert ap.radios[0].channel == 36
    assert ap.radios[0].channel_utilization_percent == 22
    assert ap.radios[1].tx_retries_percent == 12.0

    offline = parsed[1]
    assert offline.is_online is False
    assert offline.radios == []


def test_parse_devices_ignores_malformed_radio_entries() -> None:
    devices = [{"mac": "1", "name": "x", "state": 1, "radio_table_stats": ["not-a-dict", None, 5]}]
    parsed = parse_devices(devices)
    assert parsed[0].radios == []


def test_parse_health_basic() -> None:
    health = [
        {"subsystem": "wan", "status": "ok", "wan_ip": "1.2.3.4", "gw_mac": "aa:bb", "num_user": 12},
        {"subsystem": "wlan", "status": "warning"},
    ]
    parsed = parse_health(health)
    assert parsed[0].subsystem == "wan"
    assert parsed[0].num_user == 12
    assert parsed[1].status == "warning"
    assert parsed[1].num_user is None


# Trimmed from an actual live UDM-family controller's stat/health response -
# not a guess. Only the fields parse_wan_health() reads are kept.
_LIVE_WAN_HEALTH_FIXTURE = {
    "subsystem": "wan",
    "status": "ok",
    "wan_ip": "203.0.113.42",
    "isp_name": "Example Municipal Utility",
    "rx_bytes-r": 410688,
    "tx_bytes-r": 412003,
    "uptime_stats": {
        "WAN": {
            "availability": 100.0,
            "latency_average": 8,
        },
        "WAN2": {
            "availability": 0.0,
        },
    },
}


def test_parse_wan_health_extracts_isp_latency_and_live_rate() -> None:
    result = parse_wan_health([_LIVE_WAN_HEALTH_FIXTURE, {"subsystem": "wlan", "status": "ok"}])
    assert result.isp_name == "Example Municipal Utility"
    assert result.availability_percent == 100.0
    assert result.latency_ms == 8.0
    # 410688 bytes/s * 8 / 1e6 = 3.29 Mbps
    assert result.rx_rate_mbps == 3.29
    assert result.tx_rate_mbps == 3.3
    # No second WAN configured on the controller this was captured from -
    # still a real "WAN2" entry, just reporting 0% availability.
    assert result.wan2_availability_percent == 0.0


def test_parse_wan_health_no_wan_subsystem_returns_empty() -> None:
    result = parse_wan_health([{"subsystem": "wlan", "status": "ok"}])
    assert result.isp_name is None
    assert result.availability_percent is None
    assert result.latency_ms is None
    assert result.wan2_availability_percent is None


def test_parse_wan_health_missing_uptime_stats_does_not_crash() -> None:
    result = parse_wan_health([{"subsystem": "wan", "isp_name": "Some ISP"}])
    assert result.isp_name == "Some ISP"
    assert result.availability_percent is None
    assert result.latency_ms is None
    assert result.wan2_availability_percent is None


def test_parse_wan_health_no_wan2_entry_is_none_not_zero() -> None:
    # A controller with no WAN2 monitor entry at all (the common case) must
    # come back as None, not 0.0 - the distinction the Wan2AvailabilitySensor
    # relies on to go unavailable rather than showing a misleading 0%.
    health = [{"subsystem": "wan", "uptime_stats": {"WAN": {"availability": 100.0}}}]
    result = parse_wan_health(health)
    assert result.wan2_availability_percent is None


# Trimmed from an actual live UDM-family controller's stat/health response.
_LIVE_NETWORK_HEALTH_FIXTURE = [
    {
        "subsystem": "wlan",
        "status": "ok",
        "num_ap": 5,
        "num_guest": 2,
        "num_iot": 3,
    },
    {
        "subsystem": "lan",
        "status": "ok",
        "num_sw": 4,
        "num_guest": 0,
        "num_iot": 1,
    },
    {
        "subsystem": "www",
        "status": "ok",
        "drops": 2,
        "xput_down": 245.3,
        "xput_up": 41.7,
        "speedtest_ping": 9,
        "speedtest_lastrun": 1790150452,
    },
]


def test_parse_network_health_extracts_all_fields() -> None:
    result = parse_network_health(_LIVE_NETWORK_HEALTH_FIXTURE)
    assert result.connected_aps == 5
    assert result.switch_count == 4
    assert result.guest_clients == 2  # 2 (wlan) + 0 (lan)
    assert result.iot_clients == 4  # 3 (wlan) + 1 (lan)
    assert result.speedtest_download_mbps == 245.3
    assert result.speedtest_upload_mbps == 41.7
    assert result.speedtest_ping_ms == 9.0
    assert result.speedtest_last_run == 1790150452
    assert result.wan_drops == 2


def test_parse_network_health_idle_speedtest_reads_zero_not_none() -> None:
    # A controller that hasn't run a speed test recently reports 0.0, not a
    # missing field - distinguishing "never run" from "ran, got 0 Mbps"
    # isn't possible from this payload, so we surface the 0 as-is rather
    # than hiding it (speedtest_last_run tells the user how stale it is).
    result = parse_network_health([{"subsystem": "www", "xput_down": 0.0, "xput_up": 0.0}])
    assert result.speedtest_download_mbps == 0.0
    assert result.speedtest_upload_mbps == 0.0


def test_parse_network_health_missing_subsystems_returns_none() -> None:
    result = parse_network_health([])
    assert result.connected_aps is None
    assert result.switch_count is None
    assert result.guest_clients is None
    assert result.speedtest_download_mbps is None


# Trimmed from a live controller's stat/device response for one AP, one
# switch, and one gateway - only the fields the new per-device sensors read.
_LIVE_AP_DEVICE_FIXTURE = {
    "mac": "aa:bb:cc:dd:ee:01",
    "name": "U6-PRO-LIVINGROOM",
    "model": "UAP6MP",
    "type": "uap",
    "state": 1,
    "uptime": 9828411,
    "num_sta": 12,
    "satisfaction": 98,
    "system-stats": {"cpu": "4.7", "mem": "66.5", "uptime": "9828411"},
    "version": "6.8.2.15592",
    "displayable_version": "6.8.2",
    "upgradable": False,
}

_LIVE_SWITCH_DEVICE_FIXTURE = {
    "mac": "aa:bb:cc:dd:ee:02",
    "name": "Kern-Switch",
    "model": "USW-Pro-48-PoE",
    "type": "usw",
    "state": 1,
    "uptime": 500000,
    "num_sta": 33,
    "satisfaction": 92,
    "system-stats": {"cpu": "2.3", "mem": "80.2"},
    "anomalies": -1,
    "overheating": None,
    "port_table": [
        {
            "port_idx": 1,
            "up": True,
            "poe_power": "12.34",
            "speed": 1000,
            "name": "Port 1",
            "media": "GE",
            "rx_bytes-r": 779.3106246576455,
            "tx_bytes-r": 119.49443103203426,
        },
        {"port_idx": 2, "up": True, "poe_power": "0.00", "speed": 100, "name": "Port 2", "media": "GE"},
        {"port_idx": 3, "up": False, "speed": 0, "name": "Port 3", "media": "GE"},
        {"port_idx": 4, "up": True, "name": "Port 4"},  # no poe_power key at all (non-PoE port)
        {"port_idx": 5, "up": True, "speed": 10000, "name": "SFP+ Uplink", "media": "SFP+"},
    ],
}

_LIVE_GATEWAY_DEVICE_FIXTURE = {
    "mac": "aa:bb:cc:dd:ee:03",
    "name": "UXG-PRO",
    "model": "UXG-Pro",
    "type": "uxg",
    "state": 1,
    "uptime": 4301070,
    "num_sta": 33,
    "system-stats": {"cpu": "20.6", "mem": "60.3"},
    "temperatures": [
        {"name": "CPU", "type": "cpu", "value": 44.75},
        {"name": "Local", "type": "board", "value": 42.5},
    ],
    "storage": [{"mount_point": "/persistent", "size": 2040373248, "used": 15134720}],
}


def test_parse_devices_ap_metrics() -> None:
    device = parse_devices([_LIVE_AP_DEVICE_FIXTURE])[0]
    assert device.cpu_percent == 4.7
    assert device.memory_percent == 66.5
    assert device.satisfaction_percent == 98.0
    assert device.client_count == 12
    # AP-only fixture: gateway/switch-only fields stay None.
    assert device.cpu_temp_celsius is None
    assert device.storage_percent is None
    assert device.poe_power_watts is None
    assert device.active_ports is None


def test_parse_devices_satisfaction_sentinel_minus_one_is_none() -> None:
    # UniFi uses -1 as "no data yet" (e.g. a radio/device with zero
    # connected clients) - confirmed live - not a real negative percentage.
    fixture = {**_LIVE_AP_DEVICE_FIXTURE, "satisfaction": -1}
    device = parse_devices([fixture])[0]
    assert device.satisfaction_percent is None


def test_parse_devices_switch_aggregates_ports() -> None:
    device = parse_devices([_LIVE_SWITCH_DEVICE_FIXTURE])[0]
    assert device.satisfaction_percent == 92.0
    # 12.34 + 0.00 (port 3 excluded: down but has no poe_power anyway; port
    # 4 excluded: no poe_power key at all, i.e. not a PoE-capable port)
    assert device.poe_power_watts == 12.34
    assert device.active_ports == 4  # ports 1, 2, 4, 5 are up
    assert device.total_ports == 5


def test_parse_devices_switch_per_port_detail() -> None:
    device = parse_devices([_LIVE_SWITCH_DEVICE_FIXTURE])[0]
    assert len(device.ports) == 5
    port1 = device.ports[0]
    assert port1.port_idx == 1
    assert port1.name == "Port 1"
    assert port1.is_up is True
    assert port1.speed_mbps == 1000
    assert port1.poe_power_watts == 12.34
    assert port1.media == "GE"
    # 779.3106246576455 * 8 / 1e6, 119.49443103203426 * 8 / 1e6
    assert port1.rx_mbps == 0.006
    assert port1.tx_mbps == 0.001
    port4 = device.ports[3]
    assert port4.poe_power_watts is None  # no poe_power key on this port
    assert port4.rx_mbps is None  # no rx_bytes-r key on this port
    port5 = device.ports[4]
    assert port5.media == "SFP+"  # fibre/DAC uplink, not copper


def test_parse_devices_anomalies_sentinel_minus_one_is_none() -> None:
    device = parse_devices([_LIVE_SWITCH_DEVICE_FIXTURE])[0]
    assert device.anomalies is None


def test_parse_devices_anomalies_real_count() -> None:
    fixture = {**_LIVE_SWITCH_DEVICE_FIXTURE, "anomalies": 2}
    device = parse_devices([fixture])[0]
    assert device.anomalies == 2


def test_parse_devices_overheating_null_stays_none() -> None:
    # Confirmed live: seen as null (not false) when the controller has no
    # value to report, not just absent from the payload.
    device = parse_devices([_LIVE_SWITCH_DEVICE_FIXTURE])[0]
    assert device.overheating is None


def test_parse_devices_overheating_true() -> None:
    fixture = {**_LIVE_SWITCH_DEVICE_FIXTURE, "overheating": True}
    device = parse_devices([fixture])[0]
    assert device.overheating is True


def test_parse_devices_non_switch_has_no_ports() -> None:
    device = parse_devices([_LIVE_AP_DEVICE_FIXTURE])[0]
    assert device.ports == []


def test_parse_devices_firmware_up_to_date() -> None:
    device = parse_devices([_LIVE_AP_DEVICE_FIXTURE])[0]
    assert device.firmware_version == "6.8.2"  # prefers displayable_version
    assert device.firmware_latest_version == "6.8.2"  # not upgradable -> same


def test_parse_devices_firmware_update_available() -> None:
    fixture = {
        **_LIVE_AP_DEVICE_FIXTURE,
        "upgradable": True,
        "upgrade_to_firmware": "6.9.0.99999",
    }
    device = parse_devices([fixture])[0]
    assert device.firmware_version == "6.8.2"
    assert device.firmware_latest_version == "6.9.0.99999"


def test_parse_devices_firmware_upgradable_without_target_falls_back() -> None:
    # Defensive: upgradable=True but no upgrade_to_firmware value shouldn't
    # report "update to None".
    fixture = {**_LIVE_AP_DEVICE_FIXTURE, "upgradable": True, "upgrade_to_firmware": None}
    device = parse_devices([fixture])[0]
    assert device.firmware_latest_version == device.firmware_version


def test_parse_devices_firmware_missing_fields_does_not_crash() -> None:
    device = parse_devices([{"mac": "1", "name": "bare", "type": "uap", "state": 1}])[0]
    assert device.firmware_version is None
    assert device.firmware_latest_version is None


def test_parse_devices_gateway_temperature_and_storage() -> None:
    device = parse_devices([_LIVE_GATEWAY_DEVICE_FIXTURE])[0]
    assert device.cpu_temp_celsius == 44.75  # "cpu"-typed entry, not "board"
    # 15134720 / 2040373248 * 100 = 0.7...%
    assert device.storage_percent == 0.7
    # Gateway fixture has no radio_table_stats/satisfaction -> AP-only
    # fields stay None/empty, not guessed.
    assert device.satisfaction_percent is None
    assert device.radios == []


def test_parse_devices_missing_optional_sections_does_not_crash() -> None:
    device = parse_devices([{"mac": "1", "name": "bare", "type": "uxg", "state": 1}])[0]
    assert device.cpu_percent is None
    assert device.cpu_temp_celsius is None
    assert device.storage_percent is None
    assert device.poe_power_watts is None
