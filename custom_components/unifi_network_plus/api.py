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
``UniFi-API-client`` PHP library as references, not guessed. See README.md
for details and the "needs live validation" caveats -- this project was
built without access to a real controller, so exact field names for some
of the newer report attributes (e.g. WiFi connectivity success rates)
could not be confirmed against a live response and are implemented
defensively (missing keys resolve to ``None`` rather than raising).
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import aiohttp

from .const import REQUEST_TIMEOUT

_LOGGER = logging.getLogger(__name__)


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

    @property
    def base_url(self) -> str:
        """Return the controller's base URL."""
        return f"https://{self._host}:{self._port}"

    @property
    def is_unifi_os(self) -> bool:
        """Whether the controller was detected as a UniFi OS console."""
        return bool(self._is_unifi_os)

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

        try:
            async with self._session.post(
                f"{self.base_url}{login_path}",
                json=payload,
                ssl=self._verify_ssl,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
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

    async def _request(
        self,
        method: str,
        suffix: str,
        *,
        json_body: dict[str, Any] | None = None,
        retry_on_auth_failure: bool = True,
    ) -> Any:
        await self._ensure_logged_in()

        headers: dict[str, str] = {}
        if self._csrf_token and method.upper() != "GET":
            headers["X-CSRF-Token"] = self._csrf_token

        url = f"{self.base_url}{self._api_path(suffix)}"
        try:
            async with self._session.request(
                method,
                url,
                json=json_body,
                headers=headers,
                ssl=self._verify_ssl,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status == 401 and retry_on_auth_failure:
                    # Session/cookie expired - re-authenticate once and retry.
                    self._logged_in = False
                    await self.login()
                    return await self._request(
                        method, suffix, json_body=json_body, retry_on_auth_failure=False
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

        The report endpoints are queried via POST with a JSON body
        selecting the desired attributes and time range on most controller
        versions; recent versions also accept a plain GET. We use POST
        with an (optional) empty body first and fall back to GET, since
        both have been observed in the wild across controller versions.
        """
        suffix = f"stat/report/{kind}"
        try:
            return await self._request("POST", suffix, json_body={"attrs": None})
        except UniFiApiError:
            return await self._get(suffix)
