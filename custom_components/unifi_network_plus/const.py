"""Constants for the UniFi Network+ integration."""

from __future__ import annotations

DOMAIN = "unifi_network_plus"

CONF_SITE = "site"
CONF_VERIFY_SSL = "verify_ssl"

DEFAULT_SITE = "default"
DEFAULT_PORT = 443
DEFAULT_VERIFY_SSL = False
DEFAULT_SCAN_INTERVAL = 60
DEFAULT_TOP_CLIENTS = 5

CONF_SCAN_INTERVAL = "scan_interval"
CONF_TOP_CLIENTS_COUNT = "top_clients_count"

PLATFORMS: list[str] = ["sensor"]

MANUFACTURER = "Ubiquiti"

# --- API request timing -----------------------------------------------------
REQUEST_TIMEOUT = 15

# --- Report endpoint attribute keys -----------------------------------------
# UniFi "stat/report" entries carry both cumulative counters and, on most
# controller versions, a matching "-r" suffixed field holding the rate
# (bytes/second, or ms for latency) at the time of that sample. Field
# availability differs across controller versions, so all lookups in api.py
# fall back gracefully to None when a key is missing.
ATTR_WAN_RX_RATE = "wan-rx_bytes-r"
ATTR_WAN_TX_RATE = "wan-tx_bytes-r"
ATTR_WAN_RX_BYTES = "wan-rx_bytes"
ATTR_WAN_TX_BYTES = "wan-tx_bytes"
ATTR_WAN_LATENCY_AVG = "wan-latency_avg"

# Historical monthly-usage lookback window in days.
MONTHLY_USAGE_LOOKBACK_DAYS = 31
