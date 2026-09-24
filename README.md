# UniFi Network+

Home Assistant custom integration that talks **directly** to a local
Ubiquiti UniFi Network Controller / UniFi OS console. Started as a
companion to the core `unifi` integration (extra stats it doesn't expose);
the goal going forward is closing the remaining gap to become a complete
replacement, not just a "+" add-on - see [Roadmap toward feature
parity](#roadmap-toward-feature-parity).

**477 entities** as of the latest release (vs. the core `unifi`
integration's 281 in the same environment - mostly the per-port
entities below, which are disabled by default and don't clutter a fresh
install): WAN throughput, latency, ISP/availability, the controller's own
periodic ISP speed test, top clients by traffic (including VLAN/network,
signal, CCQ, SSID and channel), network-wide AP/switch/guest/IoT counts;
per physical device (AP/switch/gateway, each its own Home Assistant
device) CPU/memory/client-count/satisfaction/anomalies/overheating,
gateway temperature/storage, switch PoE draw/active-port-count and one
Firmware update entity; per-port link speed (with connection type:
copper/fibre/DAC and, when present, which network it carries) PoE power
and live download/upload throughput on **both switches and gateways**
(disabled by default - enable individual ports from Settings -> Entities
if you want them); a real-time-connection diagnostic sensor backed by the
controller's WebSocket event stream, used to trigger faster refreshes on
top of the normal poll interval (see "Polling frequency" below); and
`switch` entities to locate (blink) a device or control per-port PoE
power (write operations - see the permissions note in Setup). New
devices (a newly adopted AP or switch) are picked up automatically on the
next poll, no restart needed.

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

## Real bugs found (and fixed) against a live controller

This integration was originally built with no access to a real UniFi
controller. Once one became available, several bugs surfaced immediately -
recorded here because they're the kind of thing that's easy to reintroduce
and each one is covered by a regression test (except #4, a Home Assistant
threading rule rather than something `pytest` without a real HA instance
can catch):

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
4. **`async_track_time_interval` callbacks default to running in the
   executor thread pool, not the event loop.** The `Realtime Connection`
   diagnostic `binary_sensor` polls the WebSocket listener's `connected`
   flag every 10s and calls `async_write_ha_state()` from that callback.
   Home Assistant can't tell a plain function is loop-safe, so it played it
   safe and ran it off-thread - which then crashed on the loop-only
   `async_write_ha_state()` call every 10 seconds. Fixed by decorating the
   callback with `@homeassistant.core.callback`, which tells Home Assistant
   it's safe to run directly on the event loop. Confirmed clean against
   live HA logs (`binary_sensor.py`'s `_refresh`).

A fifth thing turned out to be a wrong assumption rather than a bug: report
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
| WAN2 Availability | `stat/health` → `uptime_stats.WAN2.availability` | %; secondary/failover WAN, same rolling monitor. Goes **unavailable** (not 0%) when the controller has no WAN2 monitor entry at all, which is the common single-WAN case |
| WAN Drops | `stat/health` → `www.drops` | count |
| ISP Name | `stat/health` → `www.isp_name` | |
| Speedtest Download / Upload / Ping | `stat/health` → `www.xput_down` / `xput_up` / `speedtest_ping` | the controller's own periodic/manual "ISP Speed Test" result - **not a continuous live measurement**, reads 0 between runs |
| Speedtest Last Run | `stat/health` → `www.speedtest_lastrun` | timestamp; use this to judge how stale the speedtest numbers above are |
| Connected Access Points | `stat/health` → `wlan.num_ap` | |
| Switches | `stat/health` → `lan.num_sw` | |
| Guest Clients / IoT Clients | `stat/health` → `wlan`+`lan`.`num_guest`/`num_iot`, summed | |
| Monthly Data Usage | `stat/report/daily.gw`, summed for the current calendar month | GB, with download/upload as attributes |
| Top Clients | `stat/sta`, ranked by `rx_bytes + tx_bytes` | state = busiest client name, full ranked list (configurable count) as an attribute, including each client's signal (dBm), CCQ (UniFi's own 0-1000 connection-quality score), SSID and channel for wireless clients, and network name/VLAN ID for all clients |
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
| Anomalies | `stat/device` → `anomalies` | all types; `-1` = "no data yet", surfaced as unavailable, same convention as Satisfaction |
| Overheating (`binary_sensor`) | `stat/device` → `overheating` | all types; seen as `null` (not `false`) on every AP tested against, so it goes **unavailable** rather than assuming "not overheating" - switches/gateway reported a real `true`/`false` |
| Firmware (`update` entity) | `stat/device` → `version`/`upgradable`/`upgrade_to_firmware` | all types; **read-only** - see below |

### Per AP radio (2.4/5/6GHz, grouped under that AP's device)

| Sensor | Source |
|---|---|
| Channel Utilization | `stat/device` → `radio_table_stats[].cu_total` |
| TX Retries | `stat/device` → `radio_table_stats[].tx_retries` |

### Per port (grouped under that device's own entry, **disabled by default**)

Applies to **both switches and gateways** - confirmed live that a UXG-PRO's
WAN/WAN2/LAN/SFP+ ports carry the same `port_table` shape as a switch's
(gateway ports just aren't PoE sources, so PoE Power there reads `None`).

| Sensor | Source |
|---|---|
| Link Speed | `stat/device` → `port_table[].speed` (only reported when the port is up); carries the port's media type (`GE` copper Gigabit, `SFP+` fibre/DAC uplink, etc.) and, when present, which network it carries (`wan`/`wan2`/`lan`, confirmed on a gateway) as attributes |
| PoE Power | `stat/device` → `port_table[].poe_power` |
| Download / Upload | `stat/device` → `port_table[].rx_bytes-r` / `tx_bytes-r`, live instantaneous rate (same "-r" convention as the WAN sensors) |

A 48-port switch would otherwise add 160+ near-identical entities most
users never look at port-by-port - the aggregated PoE Power/Active Ports
sensors on the switch device itself cover the common case. Enable
individual ports from Settings -> Devices & Services -> Entities if you
want to graph one specific port.

### Diagnostic

| Entity | Source |
|---|---|
| Realtime Connection (`binary_sensor`) | Whether the WebSocket event stream (see [Polling frequency](#polling-frequency---is-this-live)) is currently connected. Purely informational - the integration works identically either way, just faster when this is `on`. |

### Control (`switch`, write operations - see permissions note above)

| Entity | What it does |
|---|---|
| Locate | Starts/stops a device's locate (blink LED) mode. Purely cosmetic, fully reversible, enabled by default. |
| Port PoE (per switch port, **disabled by default**) | Turns PoE power on ("auto") or off for one port. This can disconnect whatever is plugged into that port (an AP, camera, ...) - only enable it for a specific port you've deliberately chosen to control. |

The Firmware update entity is **read-only**: it reports whether an update
is available (`installed_version`/`latest_version`), but does not
implement installing one. Triggering a firmware flash on network
infrastructure remotely, from Home Assistant, without a very deliberate
separate opt-in felt like more risk than this integration should take on
by default - see [Roadmap](#roadmap-toward-feature-parity) if you want
that changed.

WiFi connectivity success rate (association/authentication/DHCP/DNS %) was
investigated but **not implemented**: not present anywhere in the
`stat/health`/`stat/device` payloads captured from the test controller.
`parsing.py` is structured so it can be added the same way as the other
fields if you find it on your controller version.

## Polling frequency - is this "live"?

Mostly, yes. The integration keeps a background WebSocket connection to the
controller's real-time event feed (`wss://.../proxy/network/wss/s/<site>/events`,
the same endpoint the UniFi app itself uses) alongside its normal REST
polling (`DataUpdateCoordinator`, default 60s, configurable 15-3600s in the
integration's options). It deliberately does **not** try to parse or merge
the WebSocket payloads into the data model directly - UniFi's event schema
is only partially documented and reverse-engineering it correctly is a much
larger, more failure-prone undertaking than the REST endpoints already were
(see "Bugs found" below). Instead, any event frame (client connect/
disconnect, device state change, speed test, etc.) is treated purely as a
"something changed, refresh now" trigger, debounced by ~1.5s so a burst of
events collapses into a single REST refresh. In practice this means most
state changes show up within a couple of seconds instead of waiting for the
next poll interval.

This is strictly additive and safe to lose: the coordinator's own polling
timer is never disabled, so if the WebSocket is unavailable (older/classic
controllers, network hiccups, controller reboot) the integration transparently
falls back to plain polling with automatic reconnect (exponential backoff,
2-60s, with jitter so multiple HA restarts don't hammer the controller's
login rate limit at once). A diagnostic `binary_sensor` (*Realtime Connection* /
`binary_sensor.<host>_<site>_realtime_connected`, enabled by default)
reports whether the WebSocket is currently connected, so you can tell
at a glance whether you're getting event-triggered refreshes or have
fallen back to plain polling.

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

**Recommended: create a dedicated local account** for this integration
rather than reusing your Ubiquiti cloud/admin login. In the UniFi Network
app: Settings → Admins → Add Admin → "Restrict to local access only".
This limits the blast radius if the credentials stored in Home Assistant
were ever compromised, and avoids sending cloud SSO credentials to a
local integration at all.

**"View Only" vs. "Full Management" permissions - confirmed live:**
"View Only" is sufficient for every sensor/binary_sensor/update entity
(everything this integration only reads). The **Locate** and **PoE Port**
`switch` entities are write operations and need "Full Management"
(Admin) rights on the site - tested against a "View Only" account and
the controller correctly rejected both with HTTP 403, which this
integration surfaces as a clear log error rather than a silent failure.
If you don't plan to use those two control entities, "View Only" remains
the safer default; grant "Full Management" only if you specifically want
device locate/PoE control from Home Assistant.

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

## Roadmap toward feature parity

The intent is for this integration to eventually be a complete
replacement for the core `unifi` integration, not just a companion to it -
it's already past core's 281-entity count for this environment (477, most
of that from the disabled-by-default per-port entities), though entity
*count* alone isn't the same as feature parity - client presence tracking
and write/control operations below are the real remaining gap.
Phases, roughly in order (each phase should land with its own tests before
starting the next - this file's "bugs found" section exists because
skipping that step once already cost a debugging session):

1. **Done**: WAN throughput/latency/availability/ISP/speedtest, network-
   wide AP/switch/guest/IoT counts, per-device CPU/memory/satisfaction/
   anomalies/overheating/temperature/storage/PoE, per-radio channel/TX-retry
   stats, per-port link-speed/connection-type/PoE/throughput (disabled by
   default), per-client VLAN/CCQ/SSID/channel, per-device firmware `update`
   entities, dynamic device grouping, auto-discovery of new devices,
   WebSocket-triggered real-time refresh with polling fallback.
2. **Client presence tracking** (`device_tracker` platform) - the single
   biggest remaining gap (~72 entities in the core integration for this
   environment). Deliberately *not* done as part of the sensor/update push
   above: core `unifi`'s client-tracking has years of refined edge-case
   handling (MAC randomization, `consider_home` timeout tuning, guest vs.
   authorized clients, wired vs. wireless reconnect behavior) that's easy
   to get subtly wrong in a first pass. Needs its own focused session
   rather than being bolted onto an unrelated change.
3. **Done**: `switch` entities for device Locate (blink LED) and per-port
   PoE on/off, added with explicit user sign-off on the risk/blast-radius
   trade-off (write operations, unlike everything before this). Verified
   live: request shapes cross-checked against `aiounifi`'s own
   `DeviceLocateRequest`/`DeviceSetPoePortModeRequest` (already installed
   alongside Home Assistant), and a real attempt against the test
   controller's "View Only" account correctly came back HTTP 403 -
   confirming both entities need a "Full Management" local account
   (see Setup) and that this integration surfaces that as a clear error
   rather than a silent no-op. Not yet verified: an actual successful
   toggle against a "Full Management" account (the test account here is
   intentionally "View Only" - a live 403 is a good sign the request
   reaches the controller and is authenticated, but the write path itself
   awaits a controller with the right permissions to confirm end-to-end).
4. Validate against a classic (non-UniFi-OS) controller and a second UniFi
   OS firmware version/model to firm up the "what still needs checking"
   list above - the further this grows, the more that matters.
5. Find and wire up a real WAN Packet Loss field, if one exists on some
   controller version (`parsing.py::parse_wan_throughput`'s candidate list
   is ready for it); consider surfacing per-monitor WAN detail
   (`uptime_stats.WAN.monitors`) as attributes on the WAN Availability
   sensor.
6. Revisit whether the Firmware `update` entity should support
   `async_install` once the rest of this list is solid - see the caveat
   in the Sensors section above for why it doesn't yet.
7. WiFi network (SSID) enable/disable `switch` - deliberately deferred:
   unlike PoE (one port, one device), disabling an SSID drops every client
   connected to it at once. Revisit if wanted, with the same live-tested,
   sign-off-first approach as PoE control above.

## License

MIT - see [LICENSE](LICENSE).
