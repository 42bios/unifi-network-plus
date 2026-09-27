# UniFi Network+

Home Assistant custom integration that talks **directly** to a local
Ubiquiti UniFi Network Controller / UniFi OS console. Started as a
companion to the core `unifi` integration (extra stats it doesn't expose);
the goal going forward is closing the remaining gap to become a complete
replacement, not just a "+" add-on - see [Roadmap toward feature
parity](#roadmap-toward-feature-parity).

**946 entities** as of the latest release (vs. the core `unifi`
integration's 281 in the same environment - mostly per-client/per-port
entities, most disabled by default so a fresh install stays uncluttered):

- **Stats**: WAN throughput/latency/ISP/availability/speedtest, top
  clients by traffic (VLAN, signal, CCQ, SSID, channel), network-wide
  AP/switch/guest/IoT counts.
- **Per device** (AP/switch/gateway, each its own HA device): CPU/memory/
  client-count/satisfaction/anomalies/overheating, gateway temperature/
  storage, switch PoE draw/active-port-count, Firmware `update` entity.
- **Per port** (disabled by default, both switches and gateways): link
  speed/media type/network, PoE power, live down/upload.
- **Diagnostics**: realtime-connection and account-permission sensors,
  the latter gating every control entity below.
- **Control** (write ops, see Setup for permissions): locate/LED/PoE/
  port-enable switches, block-client, restart/reconnect buttons.
- **Presence**: a `device_tracker` per client the controller has *ever*
  seen (not just currently connected), `home`/`not_home`.

New devices/clients are picked up automatically on the next poll, no
restart needed.

**Verified against a live UDM-family (UniFi OS) controller** - see "What
was verified" below for exactly what that covered and what's still
best-effort on other controller models/versions.

## Contents

- [Why this exists](#why-this-exists)
- [Feature comparison vs. core `unifi`](#feature-comparison-vs-core-unifi)
- [How it works](#how-it-works)
- [Real bugs found (and fixed) against a live controller](#real-bugs-found-and-fixed-against-a-live-controller)
- [Sensors](#sensors)
- [Polling frequency - is this "live"?](#polling-frequency---is-this-live)
- [Auto-discovery of new devices](#auto-discovery-of-new-devices)
- [What was verified vs. what still needs checking on your controller](#what-was-verified-vs-what-still-needs-checking-on-your-controller)
- [Installation](#installation)
- [Setup](#setup)
- [Repository layout](#repository-layout)
- [Development / running tests](#development-running-tests)
- [Roadmap toward feature parity](#roadmap-toward-feature-parity)
- [On UniFi's official REST API (developer.ui.com)](#on-unifis-official-rest-api-developeruicom)
- [Bundled automation blueprints](#bundled-automation-blueprints)
- [License](#license)

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

## Feature comparison vs. core `unifi`

Read directly from core's own source (`homeassistant/components/unifi/`)
in the same Home Assistant install this integration was tested against,
not guessed or taken from marketing copy. ✅ = has it, ❌ = doesn't, only
one of the two columns is filled in per row (core's own strengths are
listed honestly, not omitted).

| Area | This integration | core `unifi` |
|---|---|---|
| WAN throughput (down/up Mbps), ISP name, WAN Availability %, WAN2, WAN Drops, monthly usage | ✅ | ❌ - only WAN latency |
| Top Clients ranked list (traffic, CCQ, SSID, channel, VLAN/network, uptime) | ✅ | ❌ |
| Device Satisfaction score, Anomalies count, Overheating status | ✅ | ❌ |
| Per-radio channel utilization / TX retries | ✅ | ❌ |
| Per-port media type (copper/fibre/DAC), "what's connected" (name/mac/ip), live throughput on **both switches and gateways** | ✅ | Partial - port bandwidth/link-speed sensors exist, but switch-only, no media type or "what's connected" |
| Individual per-client persistent bandwidth RX/TX sensor, wired client link speed | ❌ - in the Top Clients attribute list, not its own entity | ✅ |
| Client presence (`device_tracker`) | ✅ - one entity per client the controller has **ever** seen (`rest/user`, confirmed 90 vs. a handful online) | Partial - core doesn't call this endpoint at all, so it only tracks clients from its own active/recent pool |
| Account permission awareness (proactively marks control entities unavailable) | ✅ | ❌ |
| Realtime WebSocket-triggered refresh (on top of normal polling) | ✅ | Uses WebSocket events as its primary data source instead of polling - a different architecture, not directly comparable |
| Locate (blink LED, momentary) | ✅ | ❌ |
| Device LED persistent on/off | ✅ (on/off only) | ✅, as a `light` entity with brightness/color |
| Restart device | ✅ (soft only) | ✅ (soft only) |
| PoE port on/off (persistent) | ✅ | ✅ |
| PoE port power-cycle (momentary) | ❌ | ✅ |
| Port link enable/disable | ✅ | ✅ |
| Block/unblock client | ✅ | ✅ |
| Reconnect (kick) client | ✅ | ❌ |
| Firmware update - detect | ✅ | ✅ |
| Firmware update - **install** | ❌ - deliberately deferred, see [Roadmap](#roadmap-toward-feature-parity) | ✅ |
| WLAN (SSID) enable/disable, regenerate password | ❌ - deferred, see Roadmap | ✅ |
| Firewall policies, Policy Engine rules, DPI restrictions, port forwarding, traffic rules/routes | ❌ - out of scope so far | ✅ |
| UniFi smart plug (outlet) control/metering, SmartPower AC budget/consumption | ❌ - not tested against this hardware | ✅ |
| Bundled automation Blueprints | ✅ (3, optional, not enabled by default) | ❌ |

The short version: this integration goes deep on **statistics and
diagnostics** (WAN, satisfaction, anomalies, per-port/per-radio detail,
full client presence) that core doesn't expose at all, while core is
further ahead on **site-wide network configuration** (firewall, DPI,
traffic policy, VPN, smart plugs) - a different, larger scope this
integration hasn't taken on. Complement, not a strict superset, in
either direction.

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

`aiounifi` (used internally by core `unifi`) was reviewed as a base - a
good reference for auth/CSRF and endpoint paths, cross-checked alongside
`Art-of-WiFi/UniFi-API-client` while building `api.py` - but its object
model is built entirely around its own typed client/device/event
abstractions. Reaching into raw `stat/report`/`stat/health`/radio-table
data through it would mean fighting that model as much as using it, so
this integration uses a small, dependency-free (`aiohttp` only) client
instead.

## Real bugs found (and fixed) against a live controller

These surfaced during live testing against a real UDM-family controller -
recorded here because they're the kind of thing that's easy to
reintroduce. #1-3 have a regression test in `tests/test_api.py`; #4-6 are
Home Assistant framework behaviors (threading rules, entity-base-class
property overrides) that only show up against a real running HA instance.

| # | Bug | Fix |
|---|---|---|
| 1 | aiohttp's default `CookieJar` silently drops cookies for bare-IP hosts (RFC 6265) - login looked like a 200 success but the `TOKEN` cookie was never stored, so every following request 401'd. | `cookie_jar=aiohttp.CookieJar(unsafe=True)` in `config_flow.py`/`__init__.py`. |
| 2 | This controller's reverse proxy rejected plain-GET endpoints (`stat/sta`) with 401 unless `X-CSRF-Token` was present - most reference clients only send it on state-changing requests. | Send the header on every request once a token exists, regardless of method. |
| 3 | `stat/report/*.gw` POSTed with an empty body returns HTTP 200 with `data: []` - looks successful, silently returns nothing. | Always send explicit `attrs` + a `start`/`end` window. |
| 4 | `async_track_time_interval` callbacks default to the executor thread pool, not the event loop - crashed the loop-only `async_write_ha_state()` call every 10s. | Decorate the callback with `@homeassistant.core.callback`. |
| 5 | `ScannerEntity.entity_registry_enabled_default` is a `@property` that shadows `_attr_entity_registry_enabled_default` - every `device_tracker` came up disabled. | Override the property itself in `UniFiClientTracker`. |
| 6 | `_attr_has_entity_name = True` on an entity whose `device_info` is forced to `None` produced a duplicated name (`"X  X"`). | Remove `_attr_has_entity_name` - no device to compose with anyway. |

A seventh thing was a wrong assumption, not a bug: `wan-rx_bytes`/`wan-tx_bytes`
are **per-bucket totals**, not a running counter - Mbps is
`bytes * 8 / bucket_seconds`, not a delta between samples.

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

### Client presence (`device_tracker`, one per client the controller has ever seen via `rest/user`)

State is `home`/`not_home`, with `is_wired`, `is_guest`, `is_blocked`, `last_seen`, `network`, `essid` and `manufacturer` as attributes. Enabled by default - this is the whole point of the feature, unlike the per-port entities.

### Diagnostic

| Entity | Source |
|---|---|
| Realtime Connection (`binary_sensor`) | Whether the WebSocket event stream (see [Polling frequency](#polling-frequency---is-this-live)) is currently connected. Purely informational - the integration works identically either way, just faster when this is `on`. |
| Account Permission | The configured account's site role (`admin`/`readonly`/...), from `GET self/sites` - the same check core `unifi` uses for `hub.is_admin`. Lets you see at a glance whether the Control entities below can actually work, without reading logs. |

### Control (write operations - see permissions note above)

| Entity | What it does |
|---|---|
| Locate (`switch`) | Starts/stops a device's locate (blink LED) mode. Purely cosmetic, fully reversible, enabled by default. **Verified live**: actually toggled a real gateway's LED on then off. |
| LED (`switch`) | Turns a device's status LED persistently on/off (distinct from Locate's temporary blink) - the permanent "keep it dark" preference. Cosmetic, enabled by default. |
| Port PoE (`switch`, per switch port, **disabled by default**) | Turns PoE power on ("auto") or off for one port. This can disconnect whatever is plugged into that port (an AP, camera, ...) - only enable it for a specific port you've deliberately chosen to control. |
| Port Enabled (`switch`, per port, **disabled by default**) | Enables/disables a port's forwarding at the link level - same risk as Port PoE, just not power-based. A port can have both entities if it supports both. |
| Block Client (`switch`, per client, **disabled by default**) | Blocks/unblocks a client from the network entirely - the classic parental-control/access-control action. |
| Reconnect Client (`button`, per client, **disabled by default**) | Forces a connected client to disconnect and immediately re-associate - not a block, just a nudge for a client stuck on a bad AP/band. |
| Restart (`button`, per device, **disabled by default**) | Soft-restarts a device. Real, disruptive action - the device and everything connected through it briefly goes offline. |

All of the above go **unavailable** (rather than failing only when used) when the Account Permission sensor above reports anything other than `admin`.

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

Mostly. A background WebSocket connection to the controller's real-time
event feed (the same one the UniFi app uses) runs alongside normal REST
polling (default 60s, configurable 15-3600s). It doesn't parse/merge the
WebSocket payloads (UniFi's event schema is only partially documented -
see "Bugs found" for how much that already cost on the REST side);
instead any event just triggers an early REST refresh, debounced ~1.5s.
Most state changes show up within a couple of seconds instead of waiting
for the next poll.

Strictly additive and safe to lose: the coordinator's own polling timer
never stops, so if the WebSocket drops (older controllers, network
hiccups, reboot) it falls back to plain polling with automatic reconnect
(exponential backoff, jittered so multiple HA restarts don't hammer the
controller's login rate limit at once). The *Realtime Connection*
diagnostic `binary_sensor` (enabled by default) shows which mode you're
currently getting.

## Auto-discovery of new devices

Yes - `sensor.py` registers a coordinator listener, not just a one-time
entity list at startup. Every poll checks for device MACs / AP radios not
seen before and adds entities for them on the fly. Adopt a new AP or
switch and its sensors appear automatically within one poll interval - no
Home Assistant restart or re-adding the integration required.

## What was verified vs. what still needs checking on your controller

Tested end-to-end (real login, every endpoint and sensor except WAN
Packet Loss, every per-device metric across one `uap`/`usw`/`uxg`)
against one UniFi OS console (UDM-family, controller 5.1.26, single
site, 5 APs, 4 switches, 90 known clients). The write/control path was
verified live too with a "Full Management" account: Locate actually
toggled a real gateway's LED on then off. What it does *not* cover:

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
2. Repository URL: `https://github.com/42bios/unifi-network-plus`, category `Integration`.
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

**Troubleshooting:** Settings → Devices & Services → UniFi Network+ → the
three-dot menu on the integration card → "Download diagnostics" gives a
JSON snapshot of the last poll (config, site role, every parsed
sensor/device/client value) to attach to a bug report. Host, username,
password, MAC addresses, IPs, hostnames and SSIDs are all redacted before
the file is generated - nothing identifying leaves your machine.

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

The intent is a complete replacement for core `unifi`, not just a
companion - entity count alone isn't feature parity; the write/control
path and client presence were the real remaining gap, and both are done.

**Done:** all stats/diagnostics from the intro; client presence
(`device_tracker`, one per client ever seen, from `rest/user` +
`stat/sta`); Block/Reconnect Client; Locate, LED, PoE port, Port Enabled
switches and a Restart button, all gated on an **Account Permission**
sensor (site-role check, same mechanism core's `hub.is_admin` uses) so
they go unavailable up front rather than failing on first use. Verified
live: Locate actually toggled a real gateway's LED on then off.

**Not done, deliberately:**

| Item | Why deferred |
|---|---|
| Firmware `update.async_install` | Mechanism is confirmed (`cmd/devmgr` upgrade), but a remote firmware flash is the highest-risk write this integration could do - needs its own explicit sign-off. |
| WiFi SSID enable/disable | Drops every client on that network at once (unlike one PoE port); would also need UniFi's API-key REST API, not the legacy endpoints used elsewhere. |
| PoE power-cycle (momentary) | Persistent on/off exists; the momentary variant hasn't been added. |
| WAN Packet Loss | No matching field found in the tested controller's responses at all - `parsing.py` has a candidate list ready if one turns up on another firmware version. |
| Validate on a classic (non-UniFi-OS) controller / other firmware versions | Only unit-tested against mocked responses so far, not a real classic controller. |
| Firewall/DPI/traffic policy, smart plug control | Out of scope so far - a materially larger surface than device/client control. |

## On UniFi's official REST API (developer.ui.com)

UniFi Network ships an official, versioned, API-key-authenticated REST
API (`X-API-KEY` header, separate from the username/password this
integration otherwise uses) - evaluated as a possible replacement for
some of the reverse-engineered legacy endpoints above, via its published
OpenAPI spec. Decided against adopting it for anything this integration
already does: its device/port/client "actions" are much more limited
than the legacy API (`RESTART` only for devices - no locate; `POWER_CYCLE`
only for ports - no persistent on/off; guest-portal authorize/
unauthorize only for clients - no block/reconnect), and its device
statistics endpoint is leaner than `stat/device` (no satisfaction,
anomalies, overheating, or per-port detail). It's a good fit for exactly
one thing this integration doesn't do yet: WiFi Broadcast (SSID)
management has a clean, official `enabled` field - see item 8 above if
that gets built.

## Bundled automation blueprints

Three ready-made [blueprints](https://www.home-assistant.io/docs/automation/using_blueprints/)
ship under `blueprints/automation/unifi_network_plus/` - optional, not
enabled by anything on their own. Import one from Settings -> Automations
-> Blueprints -> Import Blueprint, using the raw GitHub URL of the file,
or copy it into your own `config/blueprints/automation/` folder.

| Blueprint | What it does |
|---|---|
| `device_problem_alert.yaml` | Notify when any of the Overheating binary_sensors turns on, or an Anomalies sensor rises above zero. |
| `client_presence_notify.yaml` | Notify when one or more `device_tracker` clients arrive home or leave (each direction toggleable). |
| `wan_outage_alert.yaml` | Notify when WAN Availability drops below a threshold for a sustained period (avoids false alarms from one brief blip). |

Each takes a generic "notification action" input (any action(s) you want -
a mobile app notification, a TTS announcement, whatever), with a
ready-made `{{ alert_message }}`/`{{ presence_message }}`/
`{{ availability_message }}` variable you can drop into your message text.
Verified against Home Assistant's real blueprint-loading code path (schema
validation + input substitution, not just YAML syntax) with representative
dummy inputs for all three - not just eyeballed.

## License

MIT - see [LICENSE](LICENSE).
