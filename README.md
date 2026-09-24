# UniFi Network+

Home Assistant custom integration that talks **directly** to a local
Ubiquiti UniFi Network Controller / UniFi OS console and exposes the extra
statistics the built-in core `unifi` integration does not: WAN throughput,
latency, ISP/availability, the controller's own periodic ISP speed test,
top clients by traffic, network-wide AP/switch/guest/IoT counts, and - per
physical device (AP/switch/gateway), each grouped as its own Home Assistant
device - CPU/memory/client-count/satisfaction, plus gateway temperature/
storage and switch PoE draw/active-port-count. New devices (a newly adopted
AP or switch) are picked up automatically on the next poll, no restart
needed.

**Verified against a live UDM-family (UniFi OS) controller** - see "What
was verified" below for exactly what that covered and what's still
best-effort on other controller models/versions.

## Why this exists

The core `unifi` integration is great for device presence and basic
device health, but it does not map several of the numbers the UniFi
Network application itself shows on its dashboard:

- WAN throughput history (the "Internet Activity" download/upload graph)
- Average WAN latency and the controller's own WAN availability monitor
- ISP name
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
  payload) that is then sent back on every subsequent request - **on every
  request, not just state-changing ones**, see the bugs section below for
  why that matters.
- Reads data from `stat/sta` (clients), `stat/device` (APs/switches/
  gateways, including per-radio stats), `stat/health` (subsystem status,
  including WAN ISP/latency/availability), and
  `stat/report/{5minutes,hourly,daily}.gw` (WAN throughput/usage history),
  scoped under `/api/s/<site>/...` (classic) or
  `/proxy/network/api/s/<site>/...` (UniFi OS).
- Polls on a `DataUpdateCoordinator` (default 60s, configurable) and
  exposes the parsed results as sensors, grouped as Home Assistant devices
  per physical UniFi device (each AP gets its own device entry, linked to
  the controller device via `via_device`). Historical charts for the
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

## Three real bugs found (and fixed) against a live controller

This integration was originally built with no access to a real UniFi
controller. Once one became available, three bugs surfaced immediately -
recorded here because they're the kind of thing that's easy to reintroduce
and each one is covered by a regression test:

1. **aiohttp silently drops cookies for bare-IP hosts.** UniFi controllers
   are almost always reached by LAN IP (`192.168.x.x`), not a hostname.
   aiohttp's default `CookieJar` refuses to store cookies for numeric-IP
   hosts unless constructed with `unsafe=True` - a conservative RFC 6265
   read. The login call itself looked completely successful (HTTP 200,
   `Set-Cookie: TOKEN=...`), but the cookie was never actually stored, so
   *every* following request came back a plain 401. Fixed by passing
   `cookie_jar=aiohttp.CookieJar(unsafe=True)` to `async_create_clientsession`
   in both `config_flow.py` and `__init__.py`. See
   `tests/test_api.py::test_default_aiohttp_cookie_jar_drops_cookies_for_ip_hosts`.
2. **CSRF token required on GET, not just POST/PUT/DELETE.** This
   controller's `/proxy/network/...` reverse proxy rejected `stat/sta` (a
   plain GET) with a 401 unless `X-CSRF-Token` was present, even though
   the login itself had already succeeded and most reference clients only
   attach it to state-changing requests. Fixed by sending the header on
   every request once we have a token, regardless of method. See
   `tests/test_api.py::test_csrf_header_sent_on_get_requests_too`.
3. **`stat/report/*.gw` needs an explicit `attrs` + `start`/`end` body.**
   POSTing with an empty/`None` body returns HTTP 200 with an empty
   `data: []` - it *looks* successful, so this was the hardest of the
   three to notice. Fixed by always sending `attrs: ["time", "wan-rx_bytes",
   "wan-tx_bytes", "wan-latency_avg"]` plus a `start`/`end` window sized to
   the report granularity. See
   `tests/test_api.py::test_report_request_includes_attrs_and_time_range`.

A fourth thing turned out to be a wrong assumption rather than a bug: report
samples' `wan-rx_bytes` / `wan-tx_bytes` are **per-bucket totals** (bytes
transferred during that one 5-minute/daily window), not a running lifetime
counter - so Mbps is `bytes * 8 / bucket_seconds`, not a delta between two
samples the way some other UniFi API clients' docs describe it.

## Sensors

All entries below are confirmed against a live UDM-family controller
unless noted. Per-device and per-radio sensors are created dynamically -
the counts shown are for the test setup (5 APs, 4 switches, 1 gateway).

### Controller-wide

| Sensor | Source | Notes |
|---|---|---|
| WAN Download / WAN Upload | `stat/report/5minutes.gw`, latest bucket | Mbps |
| WAN Latency | `stat/health` → `uptime_stats.WAN.latency_average` | ms (the report endpoint carries no latency field on the tested controller) |
| WAN Packet Loss | `stat/report/5minutes.gw` | %; **not confirmed** - no matching field found in the live response tested against, see below |
| WAN Availability | `stat/health` → `uptime_stats.WAN.availability` | %; the controller's own rolling ping/DNS-monitor success rate |
| WAN Drops | `stat/health` → `www.drops` | count |
| ISP Name | `stat/health` → `www.isp_name` | |
| Speedtest Download / Upload / Ping | `stat/health` → `www.xput_down` / `xput_up` / `speedtest_ping` | the controller's own periodic/manual "ISP Speed Test" result - **not a continuous live measurement**, reads 0 between runs |
| Speedtest Last Run | `stat/health` → `www.speedtest_lastrun` | timestamp; use this to judge how stale the speedtest numbers above are |
| Connected Access Points | `stat/health` → `wlan.num_ap` | |
| Switches | `stat/health` → `lan.num_sw` | |
| Guest Clients / IoT Clients | `stat/health` → `wlan`+`lan`.`num_guest`/`num_iot`, summed | |
| Monthly Data Usage | `stat/report/daily.gw`, summed for the current calendar month | GB, with download/upload as attributes |
| Top Clients | `stat/sta`, ranked by `rx_bytes + tx_bytes` | state = busiest client name, full ranked list (configurable count) as an attribute |
| Connected Clients | `stat/sta` | count |

### Per physical device (its own Home Assistant device entry)

| Sensor | Source | Applies to |
|---|---|---|
| CPU / Memory | `stat/device` → `system-stats.{cpu,mem}` | all types |
| Clients | `stat/device` → `num_sta` | all types |
| Satisfaction | `stat/device` → `satisfaction` | APs, switches (UniFi's own network-experience score; `-1` = "no data yet", surfaced as unavailable rather than a fake -1%) |
| CPU Temperature | `stat/device` → `temperatures[type=cpu]` | gateway |
| Storage | `stat/device` → `storage[0]` | gateway |
| PoE Power | `stat/device` → `port_table[].poe_power`, summed | switches |
| Active Ports | `stat/device` → `port_table[].up`, counted | switches |

### Per AP radio (2.4/5/6GHz, grouped under that AP's device)

| Sensor | Source |
|---|---|
| Channel Utilization | `stat/device` → `radio_table_stats[].cu_total` |
| TX Retries | `stat/device` → `radio_table_stats[].tx_retries` |

Deliberately **not** one sensor per switch port (a 48-port switch would add
48 near-identical entities for little benefit) - PoE Power and Active Ports
above are the aggregated view instead.

WiFi connectivity success rate (association/authentication/DHCP/DNS %) was
investigated but **not implemented**: not present anywhere in the
`stat/health`/`stat/device` payloads captured from the test controller.
`parsing.py` is structured so it can be added the same way as the other
fields if you find it on your controller version.

## Polling frequency - is this "live"?

No - this is REST polling on a timer (`DataUpdateCoordinator`, default 60s,
configurable 15-3600s in the integration's options), not a push/websocket
feed. The UniFi controller does expose a real-time WebSocket event stream
(`wss://.../proxy/network/wss/s/<site>/events`, used internally by
`aiounifi`/the core `unifi` integration for instant client connect/
disconnect events) - this integration doesn't use it, so don't expect
sub-second updates. For the kind of data this integration adds (WAN
throughput trends, device health, radio stats), a 60s poll is a reasonable
default; lower it if you want more granularity at the cost of more requests
against the controller.

## Auto-discovery of new devices

Yes - `sensor.py` registers a coordinator listener, not just a one-time
entity list at startup. Every poll checks for device MACs / AP radios not
seen before and adds entities for them on the fly. Adopt a new AP or
switch and its sensors appear automatically within one poll interval - no
Home Assistant restart or re-adding the integration required.

## What was verified vs. what still needs checking on your controller

This was tested end-to-end (real login, all endpoints, all sensors except
WAN Packet Loss populated with live data, including every per-device metric
across one `uap`, one `usw` and the `uxg` gateway) against one UniFi OS
console (UDM-family, controller version 5.1.26, single site, 5 APs, 4
switches). That covers the auth flow, CSRF handling, and every endpoint's
*shape* well. What it does not cover:

- Classic (non-UniFi-OS) controllers / Cloud Key Gen1 - the classic
  `/api/login` + `/api/s/<site>/...` code path is implemented and unit
  tested against mocked responses, but not exercised against a real
  classic controller.
- Other UniFi OS console models/firmware versions - field names
  (`radio_table_stats.cu_total`/`tx_retries`, `wan-rx_bytes`, `isp_name`,
  `uptime_stats.WAN.*`) may differ on older/newer firmware.
- **WAN Packet Loss**: no field for this was found in the tested
  controller's `stat/report` or `stat/health` responses at all (only WAN
  *latency* and *availability* were present). The sensor is implemented
  defensively (stays `unavailable` rather than showing 0), but the field
  name list in `parsing.py::parse_wan_throughput` is still a guess.

If any of your sensors stay `unavailable` or look wrong, enable debug
logging (`custom_components.unifi_network_plus: debug` in
`configuration.yaml`) and compare against the UniFi Network app's own
dashboard, then adjust the field-name candidates in `parsing.py`.

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
access only", "View Only" permissions on Network/Control Plane is
sufficient for everything this integration reads. This limits the blast
radius if the credentials stored in Home Assistant were ever compromised,
and avoids sending cloud SSO credentials to a local integration at all.

Note: the controller applies a short login-attempt rate limit
(`AUTHENTICATION_FAILED_LIMIT_REACHED`, HTTP 429) after a handful of failed
logins in quick succession - if you hit that while testing credentials,
wait a minute or two before retrying rather than repeatedly resubmitting
the config flow.

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
  - `brand/` - the integration's icon/logo (reused from the official UniFi
    brand assets Home Assistant already ships for the core `unifi`
    integration, since this is the same product family - not a new logo)
- `tests/` - `pytest` unit tests (parsing + mocked-HTTP API client), no live controller required
- `hacs.json`, `LICENSE`, `.github/workflows/` - HACS/CI plumbing

## Development / running tests

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-test.txt
pytest -q
```

## Roadmap

1. Validate against a classic (non-UniFi-OS) controller and a second UniFi
   OS firmware version/model to firm up the "what still needs checking"
   list above.
2. Find and wire up a real WAN Packet Loss field, if one exists on some
   controller version (`parsing.py::parse_wan_throughput`'s candidate list
   is ready for it).
3. Consider surfacing the per-monitor detail already present in
   `uptime_stats.WAN.monitors` (e.g. individual ping.ui.com/1.1.1.1/
   8.8.8.8 latency, and the WAN2 failover subsystem seen on dual-WAN
   setups) as attributes on the WAN Availability sensor.

## License

MIT - see [LICENSE](LICENSE).
