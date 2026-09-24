"""Minimal local API client for the UniFi Network Controller.

This is a small, purpose-built async client instead of a wrapper around the
``aiounifi`` package. ``aiounifi`` (used internally by Home Assistant's core
``unifi`` integration) is tightly modeled around *its own* entity/event
abstractions aimed at device/client tracking; bending it to also expose raw
controller report/health/radio data would mean fighting its object model as
much as using it. Talking to the small set of REST endpoints we need
directly keeps this integration's dependency footprint at just ``aiohttp``
(already bundled with Home Assistant) and makes the extra data we expose
easy to trace back to a concrete HTTP response.

Endpoint paths and the UniFi-OS auth/CSRF flow were verified against the
``aiounifi`` source (Kane610/aiounifi) and the widely used Art-of-WiFi
``UniFi-API-client`` PHP library as references, not guessed, and this
client has since been exercised end-to-end against a live UDM-family
(UniFi OS) controller - see README.md for exactly what that covered, the
three real bugs that testing surfaced (aiohttp dropping cookies for
bare-IP hosts, CSRF being required on GET too, and ``stat/report`` needing
an explicit ``attrs``/time-range body), and which fields (e.g. WAN packet
loss, and anything on classic non-UniFi-OS controllers) are still
unconfirmed and implemented defensively (missing keys resolve to ``None``
rather than raising).
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import aiohttp

from .const import REQUEST_TIMEOUT

_LOGGER = logging.getLogger(__name__)

# Attributes requested from stat/report/{interval}.gw: cumulative rx/tx byte
# counters plus average WAN latency for the sample interval. Without an
# explicit "attrs" list (and a start/end range) the controller was observed
# to return an empty ``data: []`` array with HTTP 200 - i.e. it *looks*
# successful but yields nothing to parse. parsing.py checks several known
# field-name variants (e.g. a possible ``wan-rx_bytes-r`` rate field some
# controller versions add) since the exact set can vary by version.
_REPORT_ATTRS = ["time", "wan-rx_bytes", "wan-tx_bytes", "wan-latency_avg"]

# How far back to look per report interval. The controller has finite
# retention per granularity (5-minute samples aren't kept for a full year,
# for instance), so these are deliberately short windows - we only ever
# need the *latest* sample from the 5-minute/hourly reports, and the daily
# report only needs to cover the current calendar month
# (MONTHLY_USAGE_LOOKBACK_DAYS in coordinator.py already widens that window
# on the parsing side; this just has to be at least that long).
_REPORT_LOOKBACK_HOURS = {"5minutes.gw": 3, "hourly.gw": 48, "daily.gw": 24 * 35}


def _extract_csrf_from_jwt(token: str) -> str | None:
    """Best-effort extraction of a csrfToken claim from the UniFi OS TOKEN cookie.

    The ``TOKEN`` cookie set by UniFi OS is a JWT whose payload historically
    carries a ``csrfToken`` claim. This is a defensive fallback only used
    when the login response did not carry an ``X-CSRF-Token`` header.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        value = payload.get("csrfToken")
        return str(value) if value else None
    except (ValueError, TypeError, KeyError):
        return None


class UniFiApiError(Exception):
    """Generic API error."""


class UniFiAuthError(UniFiApiError):
    """Raised when authentication fails (bad credentials)."""


class UniFiConnectionError(UniFiApiError):
    """Raised when the controller cannot be reached."""


class UniFiClient:
    """Talks to a local UniFi Network Controller / UniFi OS console.

    Handles both the classic "Cloud Key Gen1 / self-hosted controller" API
    (``/api/login``, ``/api/s/<site>/...``) and the UniFi-OS-based consoles
    (UDM/UDM-Pro/UDR/Cloud Key Gen2+), which sit behind a reverse proxy at
    ``/proxy/network/...`` and require a CSRF token on state-changing
    requests plus a ``TOKEN`` cookie obtained from ``/api/auth/login``.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
        site: str = "default",
        verify_ssl: bool = False,
        port: int = 443,
    ) -> None:
        self._session = session
        self._host = host.strip().rstrip("/")
        self._username = username
        self._password = password
        self.site = site or "default"
        self._verify_ssl = verify_ssl
        self._port = port
        self._is_unifi_os: bool | None = None
        self._csrf_token: str | None = None
        self._logged_in = False
        self.site_role: str | None = None

    @property
    def base_url(self) -> str:
        """Return the controller's base URL."""
        return f"https://{self._host}:{self._port}"

    @property
    def is_unifi_os(self) -> bool:
        """Whether the controller was detected as a UniFi OS console."""
        return bool(self._is_unifi_os)

    @property
    def session(self) -> aiohttp.ClientSession:
        """The underlying aiohttp session (for the WebSocket event listener,
        which needs to reuse the same authenticated cookie jar)."""
        return self._session

    @property
    def verify_ssl(self) -> bool:
        return self._verify_ssl

    @property
    def events_url(self) -> str:
        """WebSocket URL for the controller's real-time event stream.

        Not yet verified against a live controller (unlike the REST paths
        elsewhere in this file) - paths follow the same UniFi-OS-vs-classic
        split as ``_api_path``, cross-checked against ``aiounifi``'s
        equivalent, but see README for the "needs live confirmation" note
        until a session has actually been observed connecting.
        """
        ws_base = self.base_url.replace("https://", "wss://", 1)
        if self._is_unifi_os:
            return f"{ws_base}/proxy/network/wss/s/{self.site}/events"
        return f"{ws_base}/wss/s/{self.site}/events?clients=v2"

    def auth_headers(self) -> dict[str, str]:
        """Headers to send on the WebSocket upgrade request."""
        return {"X-CSRF-Token": self._csrf_token} if self._csrf_token else {}

    async def ensure_logged_in(self) -> None:
        """Public wrapper so callers outside this module (the WebSocket
        listener) can make sure a session/CSRF token exists before
        connecting, without reaching into the private ``_ensure_logged_in``.
        """
        await self._ensure_logged_in()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def _detect_unifi_os(self) -> bool:
        """Detect whether the target is a UniFi-OS console.

        UniFi OS consoles answer the bare root URL with HTTP 200 (a login
        SPA). Classic controllers (or the direct controller port 8443 on
        older Cloud Key Gen1 / self-hosted setups) do not serve that route
        the same way, so a non-200 / connection issue is treated as
        "classic" and we fall back to the legacy login/API paths.
        """
        try:
            async with self._session.get(
                self.base_url,
                ssl=self._verify_ssl,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                allow_redirects=True,
            ) as resp:
                return resp.status == 200
        except aiohttp.ClientError as err:
            _LOGGER.debug("UniFi OS detection request failed, assuming classic controller: %s", err)
            return False

    async def login(self) -> None:
        """Authenticate against the controller.

        Raises:
            UniFiAuthError: on bad credentials (HTTP 400/401/403).
            UniFiConnectionError: on network-level failures.
        """
        if self._is_unifi_os is None:
            self._is_unifi_os = await self._detect_unifi_os()

        login_path = "/api/auth/login" if self._is_unifi_os else "/api/login"
        payload = {
            "username": self._username,
            "password": self._password,
            "remember": True,
        }
        _LOGGER.debug(
            "UniFi login: host=%s is_unifi_os=%s path=%s",
            self.base_url,
            self._is_unifi_os,
            login_path,
        )

        try:
            async with self._session.post(
                f"{self.base_url}{login_path}",
                json=payload,
                ssl=self._verify_ssl,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                _LOGGER.debug(
                    "UniFi login response: status=%s has_csrf_header=%s has_token_cookie=%s",
                    resp.status,
                    "X-CSRF-Token" in resp.headers,
                    "TOKEN" in resp.cookies,
                )
                if resp.status in (400, 401, 403):
                    raise UniFiAuthError(f"Login rejected with HTTP {resp.status}")
                if resp.status >= 400:
                    raise UniFiApiError(f"Unexpected login HTTP status {resp.status}")

                csrf = resp.headers.get("X-CSRF-Token") or resp.headers.get("x-csrf-token")
                if csrf:
                    self._csrf_token = csrf
                elif self._is_unifi_os:
                    # Some UniFi OS versions set the CSRF token as a
                    # readable (non-HttpOnly) cookie instead of a header.
                    cookie = self._session.cookie_jar.filter_cookies(self.base_url).get("TOKEN")
                    if cookie is not None:
                        self._csrf_token = _extract_csrf_from_jwt(cookie.value)
        except aiohttp.ClientError as err:
            raise UniFiConnectionError(f"Could not reach controller: {err}") from err

        self._logged_in = True

    async def _ensure_logged_in(self) -> None:
        if not self._logged_in:
            await self.login()

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    def _api_path(self, suffix: str) -> str:
        """Build the site-scoped API path for the current controller kind."""
        suffix = suffix.lstrip("/")
        if self._is_unifi_os:
            return f"/proxy/network/api/s/{self.site}/{suffix}"
        return f"/api/s/{self.site}/{suffix}"

    def _self_path(self, suffix: str) -> str:
        """Build a global (non-site-scoped) API path, e.g. self/sites."""
        suffix = suffix.lstrip("/")
        if self._is_unifi_os:
            return f"/proxy/network/api/{suffix}"
        return f"/api/{suffix}"

    async def _request(
        self,
        method: str,
        suffix: str,
        *,
        json_body: dict[str, Any] | None = None,
        retry_on_auth_failure: bool = True,
        site_scoped: bool = True,
    ) -> Any:
        await self._ensure_logged_in()

        headers: dict[str, str] = {}
        if self._csrf_token:
            # Some UniFi OS versions only enforce the CSRF header on
            # state-changing requests, others reject *any* proxied
            # /proxy/network/... call (including GET) without it. Sending it
            # unconditionally is harmless when it's not required.
            headers["X-CSRF-Token"] = self._csrf_token

        path = self._api_path(suffix) if site_scoped else self._self_path(suffix)
        url = f"{self.base_url}{path}"
        stored_cookies = self._session.cookie_jar.filter_cookies(self.base_url)
        _LOGGER.debug(
            "UniFi request: %s %s csrf_present=%s cookie_jar_has_token=%s headers_sent=%s",
            method,
            url,
            bool(self._csrf_token),
            "TOKEN" in stored_cookies,
            list(headers.keys()),
        )
        try:
            async with self._session.request(
                method,
                url,
                json=json_body,
                headers=headers,
                ssl=self._verify_ssl,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status >= 400:
                    body_preview = (await resp.text())[:500]
                    _LOGGER.debug(
                        "UniFi request failed: %s %s status=%s body=%s",
                        method,
                        suffix,
                        resp.status,
                        body_preview,
                    )
                if resp.status == 401 and retry_on_auth_failure:
                    # Session/cookie expired - re-authenticate once and retry.
                    self._logged_in = False
                    await self.login()
                    return await self._request(
                        method,
                        suffix,
                        json_body=json_body,
                        retry_on_auth_failure=False,
                        site_scoped=site_scoped,
                    )
                if resp.status == 401:
                    raise UniFiAuthError(f"Unauthorized after re-login on {suffix}")
                if resp.status >= 400:
                    raise UniFiApiError(f"HTTP {resp.status} for {suffix}")
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise UniFiConnectionError(f"Request to {suffix} failed: {err}") from err

        if not isinstance(data, dict):
            return []
        return data.get("data", [])

    async def _get(self, suffix: str) -> list[dict[str, Any]]:
        return await self._request("GET", suffix)

    # ------------------------------------------------------------------
    # Write/control endpoints - request shapes cross-checked against
    # aiounifi's DeviceLocateRequest/DeviceSetPoePortModeRequest (the same
    # library Home Assistant's core "unifi" integration uses), which is
    # already installed alongside Home Assistant - not guessed.
    # ------------------------------------------------------------------

    async def set_locate(self, mac: str, enable: bool) -> None:
        """Start/stop a device's locate (blink LED) mode.

        Purely cosmetic - no functional effect on the device.
        """
        await self._request(
            "POST",
            "cmd/devmgr",
            json_body={"cmd": "set-locate" if enable else "unset-locate", "mac": mac},
        )

    async def set_port_poe_mode(self, switch_mac: str, port_idx: int, mode: str) -> None:
        """Set one switch port's PoE mode ("auto", "off", "24v", "passthrough").

        Read-modify-write: re-fetches the current device (including its
        live ``port_overrides``) immediately before writing, rather than
        reusing a possibly-stale coordinator snapshot, and only touches the
        one port's ``poe_mode`` - every other port's override is sent back
        unchanged, matching aiounifi's approach of never reconstructing
        overrides from scratch.
        """
        devices = await self.get_devices()
        device = next((d for d in devices if d.get("mac") == switch_mac), None)
        if device is None:
            raise UniFiApiError(f"Device {switch_mac} not found")
        device_id = device.get("_id")
        if not device_id:
            raise UniFiApiError(f"Device {switch_mac} has no _id")

        new_overrides: list[dict[str, Any]] = []
        found = False
        for override in device.get("port_overrides") or []:
            override = dict(override)
            if override.get("port_idx") == port_idx:
                override["poe_mode"] = mode
                found = True
            new_overrides.append(override)

        if not found:
            new_override: dict[str, Any] = {"port_idx": port_idx, "poe_mode": mode}
            port_table = device.get("port_table") or []
            port_entry = next((p for p in port_table if p.get("port_idx") == port_idx), None)
            if port_entry and port_entry.get("portconf_id"):
                new_override["portconf_id"] = port_entry["portconf_id"]
            new_overrides.append(new_override)

        await self._request(
            "PUT",
            f"rest/device/{device_id}",
            json_body={"port_overrides": new_overrides},
        )

    # ------------------------------------------------------------------
    # Public data endpoints
    # ------------------------------------------------------------------

    async def get_clients(self) -> list[dict[str, Any]]:
        """Return currently connected clients (``stat/sta``)."""
        return await self._get("stat/sta")

    async def get_devices(self) -> list[dict[str, Any]]:
        """Return UniFi devices (APs/switches/gateways) with detail (``stat/device``)."""
        return await self._get("stat/device")

    async def get_health(self) -> list[dict[str, Any]]:
        """Return subsystem health entries (``stat/health``)."""
        return await self._get("stat/health")

    async def get_site_role(self) -> str | None:
        """Return this account's role for the configured site ("admin",
        "readonly", ...), or ``None`` if it can't be determined.

        ``GET self/sites`` is a global endpoint (not scoped under
        ``/s/<site>/...`` like everything else in this file) that lists
        every site this account can see, each with its own ``role`` -
        cross-checked against aiounifi's ``SiteListRequest``/``Site.role``,
        which Home Assistant's core ``unifi`` integration uses the exact
        same way (``hub.is_admin = site.role == "admin"``) to gate
        admin-only features. Used here so write-capable entities (Locate,
        PoE control) can reflect a "View Only" account's actual
        permissions up front instead of only discovering it via a failed
        write. Best-effort: returns None on any error rather than raising,
        since this is a nice-to-have check, not a required one.
        """
        try:
            sites = await self._request("GET", "self/sites", site_scoped=False)
        except UniFiApiError:
            return None
        if not isinstance(sites, list):
            return None
        for entry in sites:
            if isinstance(entry, dict) and entry.get("name") == self.site:
                role = entry.get("role")
                self.site_role = str(role) if role else None
                return self.site_role
        return None

    async def get_wan_report_5min(self) -> list[dict[str, Any]]:
        """Return recent 5-minute WAN throughput/latency samples."""
        return await self._get_report("5minutes.gw")

    async def get_wan_report_hourly(self) -> list[dict[str, Any]]:
        """Return hourly WAN throughput/latency samples."""
        return await self._get_report("hourly.gw")

    async def get_wan_report_daily(self) -> list[dict[str, Any]]:
        """Return daily WAN throughput samples (used for monthly usage)."""
        return await self._get_report("daily.gw")

    async def _get_report(self, kind: str) -> list[dict[str, Any]]:
        """POST to the stat/report endpoint.

        The report endpoints require an explicit ``attrs`` list and a
        ``start``/``end`` time range (in epoch milliseconds) - without them
        the controller returns HTTP 200 with an empty ``data`` array rather
        than an error, which silently looks like "no data yet" instead of
        the malformed-request it actually is.
        """
        suffix = f"stat/report/{kind}"
        now_ms = int(time.time() * 1000)
        lookback_hours = _REPORT_LOOKBACK_HOURS.get(kind, 24)
        body = {
            "attrs": _REPORT_ATTRS,
            "start": now_ms - lookback_hours * 3600 * 1000,
            "end": now_ms,
        }
        try:
            result = await self._request("POST", suffix, json_body=body)
        except UniFiApiError:
            result = await self._get(suffix)
        _LOGGER.debug("UniFi report %s: %d sample(s), first=%s", suffix, len(result), result[:1])
        return result
