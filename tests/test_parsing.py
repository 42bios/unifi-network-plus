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


def test_parse_wan_health_no_wan_subsystem_returns_empty() -> None:
    result = parse_wan_health([{"subsystem": "wlan", "status": "ok"}])
    assert result.isp_name is None
    assert result.availability_percent is None
    assert result.latency_ms is None


def test_parse_wan_health_missing_uptime_stats_does_not_crash() -> None:
    result = parse_wan_health([{"subsystem": "wan", "isp_name": "Some ISP"}])
    assert result.isp_name == "Some ISP"
    assert result.availability_percent is None
    assert result.latency_ms is None
