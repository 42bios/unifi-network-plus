# UniFi Network+

Home Assistant custom integration that talks **directly** to a local
Ubiquiti UniFi Network Controller / UniFi OS console and exposes the extra
statistics the built-in core `unifi` integration does not: WAN throughput,
latency/packet loss, top clients by traffic, and per-AP-radio channel
utilization / TX retries.

## Why this exists

The core `unifi` integration is great for device presence and basic
device health, but it does not map several of the numbers the UniFi
Network application itself shows on its dashboard:

- WAN throughput history (the "Internet Activity" download/upload graph)
- Average WAN latency / packet loss
- Top clients ranked by traffic volume
- Per-AP-radio channel utilization and TX retries
- Monthly WAN data usage

This was confirmed by searching Home Assistant's entity registry for an
existing `unifi` config entry (281 entities) - none of the above exist as
entities, not even disabled ones. The core integration simply does not
fetch or map that data.

## How it works

- Authenticates locally against the controller's REST API (no cloud
  account involved): `POST /api/auth/login` on UniFi OS consoles
  (UDM/UDM-Pro/UDR/Cloud Key Gen2+), or `POST /api/login` on classic
  controllers / Cloud Key Gen1. UniFi OS responses carry a CSRF token
  (`X-CSRF-Token` header, sometimes only inside the `TOKEN` cookie's JWT
  payload) that is then sent back on every subsequent request.
- Reads data from `stat/sta` (clients), `stat/device` (APs/switches/
  gateways, including per-radio stats), `stat/health` (subsystem status),
  and `stat/report/{5minutes,hourly,daily}.gw` (WAN throughput/usage
  history), scoped under `/api/s/<site>/...` (classic) or
  `/proxy/network/api/s/<site>/...` (UniFi OS).
- Polls on a `DataUpdateCoordinator` (default 60s, configurable) and
  exposes the parsed results as sensors. Historical charts for the
  numeric sensors (WAN Mbps, latency, etc.) come for free from Home
  Assistant's own Recorder/History - no custom charting needed.

### Why not just use `aiounifi`?

`aiounifi` (the library HA's own `unifi` integration uses internally) was
reviewed as a possible base. It is a solid reference for the auth/CSRF
flow and endpoint paths - and this integration's `api.py` was built by
cross-checking its source plus the widely used `Art-of-WiFi/UniFi-API-client`
PHP client - but its object model is built entirely around *its own*
typed client/device/event abstractions for device tracking. Wrapping it
just to reach into raw `stat/report`/`stat/health`/radio-table data would
mean fighting that model as much as using it. A small, dependency-free
(`aiohttp` only, already bundled with Home Assistant) client kept this
integration's surface area easy to audit and to keep in sync with what
each sensor actually reads.

## Sensors

| Sensor | Source | Notes |
|---|---|---|
| WAN Download / WAN Upload | `stat/report/5minutes.gw`, latest sample | Mbps, `SensorDeviceClass.DATA_RATE` |
| WAN Latency | `stat/report/5minutes.gw` | ms; **field name needs live confirmation**, see below |
| WAN Packet Loss | `stat/report/5minutes.gw` | %; **field name needs live confirmation** |
| Monthly Data Usage | `stat/report/daily.gw`, summed for the current calendar month | GB, with download/upload as attributes |
| Top Clients | `stat/sta`, ranked by `rx_bytes + tx_bytes` | state = busiest client name, full ranked list (configurable count) as an attribute |
| Connected Clients | `stat/sta` | count |
| `<AP> <radio> Channel Utilization` | `stat/device` → `radio_table_stats[].cu_total` | one sensor per AP per radio (2.4/5/6GHz), created dynamically |
| `<AP> <radio> TX Retries` | `stat/device` → `radio_table_stats[].tx_retries` | one sensor per AP per radio |

WiFi connectivity success rate (association/authentication/DHCP/DNS %)
and ISP-info were investigated but **not implemented**: no reliably
documented, version-stable field for them was found without a live
controller response to confirm against. `parsing.py` is structured so
they can be added the same way as the other fields once confirmed.

## What was verified vs. what still needs a real controller

This integration was built **without access to a real UniFi controller**
(no host, no credentials, no network path available in the environment it
was developed in). To keep it trustworthy despite that:

- Auth flow, CSRF handling, and endpoint path structure were verified
  against the `aiounifi` library source and the `Art-of-WiFi/UniFi-API-client`
  reference implementation, not guessed.
- All response parsing (`parsing.py`) is defensive: missing/renamed
  fields resolve to `None` instead of raising, and are unit tested against
  representative (not officially documented, best-effort) fixture
  payloads - see `tests/test_parsing.py`.
- The HTTP client (`api.py`) is unit tested against a mocked controller
  (`tests/test_api.py`, using `aioresponses`) covering: classic vs.
  UniFi-OS login, CSRF token capture, session-expiry re-login, and the
  `stat/report` POST→GET fallback.
- **Not verified**: exact field names/formats for WAN latency, packet
  loss, and WiFi success-rate metrics across current controller firmware
  versions; whether `stat/report/*.gw` accepts POST with an empty body or
  requires specific `attrs`; whether `radio_table_stats` field names
  (`cu_total`, `tx_retries`) match your controller version exactly.

**Before relying on this in production**, enable debug logging
(`custom_components.unifi_network_plus: debug` in `configuration.yaml`)
after first setup and compare the raw values against what the UniFi
Network app's dashboard shows, then open an issue/adjust `parsing.py` for
any field names that differ on your controller version.

## Installation

### HACS (custom repository)

1. HACS → Integrations → ⋮ → Custom repositories.
2. Repository URL: `https://github.com/manuel-mahr/unifi-network-plus`, category `Integration`.
3. Install "UniFi Network+" and restart Home Assistant.
4. Settings → Devices & Services → Add Integration → "UniFi Network+".

### Manual

1. Copy `custom_components/unifi_network_plus` into your Home Assistant `config/custom_components/` folder.
2. Restart Home Assistant.
3. Settings → Devices & Services → Add Integration → "UniFi Network+".

## Setup

You will need:

- The controller's host/IP (the UniFi OS console or Cloud Key, typically reachable at `https://<host>/`).
- A **local** account with (at minimum) read access to the site.
- The site name (`default` unless you use multiple sites).

**Recommended: create a dedicated local read-only account** for this
integration rather than reusing your Ubiquiti cloud/admin login. In the
UniFi Network app: Settings → Admins → Add Admin → "Restrict to local
access only", role "Viewer" (read-only) is sufficient for everything this
integration reads. This limits the blast radius if the credentials stored
in Home Assistant were ever compromised, and avoids sending cloud SSO
credentials to a local integration at all.

Config flow fields:

| Field | Notes |
|---|---|
| Host / IP | e.g. `192.168.1.1` or `unifi.local` |
| Username / Password | the local read-only account above |
| Site name | `default` unless you run multiple sites |
| Port | `443` unless customized |
| Verify SSL certificate | off by default (most controllers use a self-signed cert); enable if you have a trusted cert |
| Scan interval | seconds, default 60 |
| Number of top clients | how many entries to include in the Top Clients attribute list, default 5 |

## Repository layout

- `custom_components/unifi_network_plus/` - integration source
  - `api.py` - local REST client (auth, CSRF, endpoints)
  - `parsing.py` - pure functions turning raw JSON into typed data (unit tested)
  - `coordinator.py` - `DataUpdateCoordinator` polling glue
  - `config_flow.py`, `sensor.py`, `const.py`, `__init__.py`
- `tests/` - `pytest` unit tests (parsing + mocked-HTTP API client), no live controller required
- `hacs.json`, `LICENSE`, `.github/workflows/` - HACS/CI plumbing

## Development / running tests

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-test.txt
pytest -q
```

## Roadmap / next steps for the user

1. Add the integration against your real controller and enable debug
   logging for the first few polling cycles.
2. Compare `WAN Latency` / `WAN Packet Loss` sensor values (if they show
   up at all - they may stay `unavailable` if your controller uses
   different field names) against the UniFi Network app's dashboard, and
   report/adjust the candidate field lists in `parsing.py::parse_wan_throughput`
   if needed.
3. Check that `radio_table_stats` values for your AP models line up with
   the app's Radio tab; adjust `parsing.py::parse_devices` if a field name
   differs.
4. If useful, extend `parsing.py`/`sensor.py` with the still-missing WiFi
   connectivity success-rate and ISP-info metrics once you can see the raw
   `stat/health`/`stat/device` payload shape from your own controller.

## License

MIT - see [LICENSE](LICENSE).
