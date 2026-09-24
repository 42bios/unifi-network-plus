"""Tests for api.py's auth flow and request handling, using aioresponses
to mock the local controller's HTTP responses (no real controller
available in this environment - see README for what still needs live
validation).
"""

from __future__ import annotations

import re

import aiohttp
import pytest
from aioresponses import aioresponses


def _calls_for(mocked: aioresponses, method: str, url_suffix: str):
    """Find recorded aioresponses calls whose URL path ends with url_suffix.

    aioresponses keys ``mocked.requests`` by ``(method, yarl.URL)`` with the
    default HTTPS port stripped, which is brittle to hardcode a matching
    literal for - matching on the path suffix instead keeps these tests
    readable without depending on that formatting detail.
    """
    matches = [
        calls
        for (call_method, url), calls in mocked.requests.items()
        if call_method == method and str(url).endswith(url_suffix)
    ]
    assert matches, f"no recorded {method} call ending in {url_suffix!r}"
    return matches[0]


@pytest.mark.asyncio
async def test_login_classic_controller(api_module) -> None:
    """Root URL not 200 -> classic controller -> /api/login, no CSRF needed."""
    async with aiohttp.ClientSession() as session:
        client = api_module.UniFiClient(session, "controller.local", "user", "pass")
        with aioresponses() as mocked:
            mocked.get("https://controller.local:443/", status=404)
            mocked.post(
                "https://controller.local:443/api/login",
                status=200,
                payload={"meta": {"rc": "ok"}, "data": []},
            )
            await client.login()

        assert client.is_unifi_os is False


@pytest.mark.asyncio
async def test_login_unifi_os_captures_csrf_header(api_module) -> None:
    async with aiohttp.ClientSession() as session:
        client = api_module.UniFiClient(session, "udm.local", "user", "pass")
        with aioresponses() as mocked:
            mocked.get("https://udm.local:443/", status=200)
            mocked.post(
                "https://udm.local:443/api/auth/login",
                status=200,
                payload={"unique_id": "abc"},
                headers={"X-CSRF-Token": "csrf-123"},
            )
            await client.login()

        assert client.is_unifi_os is True
        assert client._csrf_token == "csrf-123"


@pytest.mark.asyncio
async def test_login_bad_credentials_raises_auth_error(api_module) -> None:
    async with aiohttp.ClientSession() as session:
        client = api_module.UniFiClient(session, "udm.local", "user", "wrong")
        with aioresponses() as mocked:
            mocked.get("https://udm.local:443/", status=200)
            mocked.post("https://udm.local:443/api/auth/login", status=401, payload={})
            with pytest.raises(api_module.UniFiAuthError):
                await client.login()


@pytest.mark.asyncio
async def test_get_clients_uses_unifi_os_proxy_path(api_module) -> None:
    async with aiohttp.ClientSession() as session:
        client = api_module.UniFiClient(session, "udm.local", "user", "pass", site="mysite")
        with aioresponses() as mocked:
            mocked.get("https://udm.local:443/", status=200)
            mocked.post(
                "https://udm.local:443/api/auth/login",
                status=200,
                payload={},
                headers={"X-CSRF-Token": "csrf-123"},
            )
            mocked.get(
                "https://udm.local:443/proxy/network/api/s/mysite/stat/sta",
                status=200,
                payload={"data": [{"mac": "aa:bb", "name": "phone", "rx_bytes": 1, "tx_bytes": 2}]},
            )
            clients = await client.get_clients()

        assert clients == [{"mac": "aa:bb", "name": "phone", "rx_bytes": 1, "tx_bytes": 2}]


@pytest.mark.asyncio
async def test_get_devices_uses_classic_path(api_module) -> None:
    async with aiohttp.ClientSession() as session:
        client = api_module.UniFiClient(session, "192.168.1.1", "user", "pass", site="default")
        with aioresponses() as mocked:
            mocked.get("https://192.168.1.1:443/", status=404)
            mocked.post("https://192.168.1.1:443/api/login", status=200, payload={})
            mocked.get(
                "https://192.168.1.1:443/api/s/default/stat/device",
                status=200,
                payload={"data": [{"mac": "11:22", "name": "AP1", "state": 1}]},
            )
            devices = await client.get_devices()

        assert devices[0]["name"] == "AP1"


@pytest.mark.asyncio
async def test_expired_session_triggers_relogin_and_retry(api_module) -> None:
    async with aiohttp.ClientSession() as session:
        client = api_module.UniFiClient(session, "udm.local", "user", "pass")
        with aioresponses() as mocked:
            mocked.get("https://udm.local:443/", status=200)
            # First login.
            mocked.post("https://udm.local:443/api/auth/login", status=200, payload={})
            # First attempt at stat/health: session expired.
            mocked.get(
                "https://udm.local:443/proxy/network/api/s/default/stat/health",
                status=401,
                payload={},
            )
            # Re-login triggered by the 401.
            mocked.post("https://udm.local:443/api/auth/login", status=200, payload={})
            # Retry succeeds.
            mocked.get(
                "https://udm.local:443/proxy/network/api/s/default/stat/health",
                status=200,
                payload={"data": [{"subsystem": "wan", "status": "ok"}]},
            )
            health = await client.get_health()

        assert health[0]["subsystem"] == "wan"


@pytest.mark.asyncio
async def test_connection_error_raises_unifi_connection_error(api_module) -> None:
    async with aiohttp.ClientSession() as session:
        client = api_module.UniFiClient(session, "unreachable.local", "user", "pass")
        with aioresponses() as mocked:
            mocked.get("https://unreachable.local:443/", status=200)
            mocked.post(
                "https://unreachable.local:443/api/auth/login",
                exception=aiohttp.ClientConnectionError("boom"),
            )
            with pytest.raises(api_module.UniFiConnectionError):
                await client.login()


@pytest.mark.asyncio
async def test_wan_report_falls_back_to_get_when_post_fails(api_module) -> None:
    async with aiohttp.ClientSession() as session:
        client = api_module.UniFiClient(session, "udm.local", "user", "pass")
        with aioresponses() as mocked:
            mocked.get("https://udm.local:443/", status=200)
            mocked.post("https://udm.local:443/api/auth/login", status=200, payload={})
            mocked.post(
                "https://udm.local:443/proxy/network/api/s/default/stat/report/5minutes.gw",
                status=404,
                payload={},
            )
            mocked.get(
                "https://udm.local:443/proxy/network/api/s/default/stat/report/5minutes.gw",
                status=200,
                payload={"data": [{"time": 1, "wan-rx_bytes-r": 100}]},
            )
            samples = await client.get_wan_report_5min()

        assert samples[0]["wan-rx_bytes-r"] == 100


def test_extract_csrf_from_jwt(api_module) -> None:
    import base64
    import json

    payload = base64.urlsafe_b64encode(json.dumps({"csrfToken": "abc123"}).encode()).decode().rstrip("=")
    token = f"header.{payload}.sig"
    assert api_module._extract_csrf_from_jwt(token) == "abc123"


def test_extract_csrf_from_jwt_malformed_returns_none(api_module) -> None:
    assert api_module._extract_csrf_from_jwt("not-a-jwt") is None


@pytest.mark.asyncio
async def test_csrf_header_sent_on_get_requests_too(api_module) -> None:
    """Regression test for a real bug found against a live UDM controller:

    the controller rejected GET requests (e.g. stat/sta) with a generic
    401 unless X-CSRF-Token was present, even though the UniFi API docs and
    most reference clients only send it on state-changing requests. Login
    itself always succeeded (200 + Set-Cookie), which made this look like
    a broken/expired session rather than a missing header on reads.
    """
    async with aiohttp.ClientSession() as session:
        client = api_module.UniFiClient(session, "udm.local", "user", "pass")
        with aioresponses() as mocked:
            mocked.get("https://udm.local:443/", status=200)
            mocked.post(
                "https://udm.local:443/api/auth/login",
                status=200,
                payload={},
                headers={"X-CSRF-Token": "csrf-123"},
            )
            mocked.get(
                "https://udm.local:443/proxy/network/api/s/default/stat/sta",
                status=200,
                payload={"data": []},
            )
            await client.get_clients()

            calls = _calls_for(mocked, "GET", "stat/sta")
            assert calls[-1].kwargs["headers"]["X-CSRF-Token"] == "csrf-123"


@pytest.mark.asyncio
async def test_report_request_includes_attrs_and_time_range(api_module) -> None:
    """Regression test: POSTing stat/report/*.gw without an explicit
    ``attrs`` list and ``start``/``end`` range returns HTTP 200 with an
    empty ``data: []`` on a real controller - i.e. it looks successful but
    silently yields nothing to parse, rather than raising an error we'd
    notice.
    """
    async with aiohttp.ClientSession() as session:
        client = api_module.UniFiClient(session, "udm.local", "user", "pass")
        with aioresponses() as mocked:
            mocked.get("https://udm.local:443/", status=200)
            mocked.post("https://udm.local:443/api/auth/login", status=200, payload={})
            mocked.post(
                "https://udm.local:443/proxy/network/api/s/default/stat/report/5minutes.gw",
                status=200,
                payload={"data": [{"time": 1}]},
            )
            await client.get_wan_report_5min()

            calls = _calls_for(mocked, "POST", "stat/report/5minutes.gw")
            body = calls[-1].kwargs["json"]
            assert body["attrs"] == api_module._REPORT_ATTRS
            assert body["start"] < body["end"]


@pytest.mark.asyncio
async def test_default_aiohttp_cookie_jar_drops_cookies_for_ip_hosts() -> None:
    """Documents the real root cause of the "login succeeds, every
    following request 401s" bug found against a live controller reached by
    bare IP: aiohttp's default ``CookieJar`` refuses to store cookies for
    numeric-IP hosts (a conservative RFC 6265 interpretation), so the
    UniFi OS ``TOKEN`` session cookie set on login was silently discarded.

    This isn't testable through ``UniFiClient`` in isolation (the fix lives
    in how the integration constructs its aiohttp session in
    __init__.py/config_flow.py, which import Home Assistant and can't be
    loaded in this HA-less test environment) - this test instead pins down
    the underlying aiohttp behaviour those two call sites rely on, so a
    change in that assumption (e.g. an aiohttp upgrade) fails loudly here
    instead of silently reintroducing the bug.
    """
    from yarl import URL

    ip_url = URL("https://192.168.1.10/")

    unsafe_jar = aiohttp.CookieJar(unsafe=True)
    unsafe_jar.update_cookies({"TOKEN": "abc"}, response_url=ip_url)
    assert "TOKEN" in unsafe_jar.filter_cookies(ip_url)

    default_jar = aiohttp.CookieJar()
    default_jar.update_cookies({"TOKEN": "abc"}, response_url=ip_url)
    assert "TOKEN" not in default_jar.filter_cookies(ip_url)


@pytest.mark.asyncio
async def test_set_locate_posts_correct_command(api_module) -> None:
    """Request shape cross-checked against aiounifi's DeviceLocateRequest."""
    async with aiohttp.ClientSession() as session:
        client = api_module.UniFiClient(session, "udm.local", "user", "pass")
        with aioresponses() as mocked:
            mocked.get("https://udm.local:443/", status=200)
            mocked.post(
                "https://udm.local:443/api/auth/login",
                status=200,
                payload={},
                headers={"X-CSRF-Token": "csrf-123"},
            )
            mocked.post(
                "https://udm.local:443/proxy/network/api/s/default/cmd/devmgr",
                status=200,
                payload={"data": []},
            )
            await client.set_locate("aa:bb:cc:dd:ee:ff", True)

        calls = _calls_for(mocked, "POST", "/cmd/devmgr")
        assert calls[0].kwargs["json"] == {"cmd": "set-locate", "mac": "aa:bb:cc:dd:ee:ff"}


@pytest.mark.asyncio
async def test_set_locate_off_sends_unset_command(api_module) -> None:
    async with aiohttp.ClientSession() as session:
        client = api_module.UniFiClient(session, "udm.local", "user", "pass")
        with aioresponses() as mocked:
            mocked.get("https://udm.local:443/", status=200)
            mocked.post(
                "https://udm.local:443/api/auth/login",
                status=200,
                payload={},
                headers={"X-CSRF-Token": "csrf-123"},
            )
            mocked.post(
                "https://udm.local:443/proxy/network/api/s/default/cmd/devmgr",
                status=200,
                payload={"data": []},
            )
            await client.set_locate("aa:bb:cc:dd:ee:ff", False)

        calls = _calls_for(mocked, "POST", "/cmd/devmgr")
        assert calls[0].kwargs["json"] == {"cmd": "unset-locate", "mac": "aa:bb:cc:dd:ee:ff"}


@pytest.mark.asyncio
async def test_set_port_poe_mode_updates_only_target_port(api_module) -> None:
    """The read-modify-write must send back every other port's override
    untouched (only the target port_idx's poe_mode changes), matching
    aiounifi's DeviceSetPoePortModeRequest behaviour.
    """
    async with aiohttp.ClientSession() as session:
        client = api_module.UniFiClient(session, "udm.local", "user", "pass")
        with aioresponses() as mocked:
            mocked.get("https://udm.local:443/", status=200)
            mocked.post(
                "https://udm.local:443/api/auth/login",
                status=200,
                payload={},
                headers={"X-CSRF-Token": "csrf-123"},
            )
            mocked.get(
                "https://udm.local:443/proxy/network/api/s/default/stat/device",
                status=200,
                payload={
                    "data": [
                        {
                            "mac": "sw:mac",
                            "_id": "device-id-123",
                            "port_overrides": [
                                {"port_idx": 1, "poe_mode": "auto", "name": "Port 1"},
                                {"port_idx": 9, "poe_mode": "auto", "name": "Port 9"},
                            ],
                            "port_table": [
                                {"port_idx": 1},
                                {"port_idx": 9, "portconf_id": "conf-9"},
                            ],
                        }
                    ]
                },
            )
            mocked.put(
                "https://udm.local:443/proxy/network/api/s/default/rest/device/device-id-123",
                status=200,
                payload={"data": []},
            )
            await client.set_port_poe_mode("sw:mac", 9, "off")

        calls = _calls_for(mocked, "PUT", "/rest/device/device-id-123")
        body = calls[0].kwargs["json"]
        overrides = {o["port_idx"]: o for o in body["port_overrides"]}
        assert overrides[1]["poe_mode"] == "auto"  # untouched
        assert overrides[9]["poe_mode"] == "off"  # updated


@pytest.mark.asyncio
async def test_set_port_poe_mode_adds_override_when_missing(api_module) -> None:
    """A port with no existing override entry gets a new one appended,
    carrying its portconf_id from port_table if present.
    """
    async with aiohttp.ClientSession() as session:
        client = api_module.UniFiClient(session, "udm.local", "user", "pass")
        with aioresponses() as mocked:
            mocked.get("https://udm.local:443/", status=200)
            mocked.post(
                "https://udm.local:443/api/auth/login",
                status=200,
                payload={},
                headers={"X-CSRF-Token": "csrf-123"},
            )
            mocked.get(
                "https://udm.local:443/proxy/network/api/s/default/stat/device",
                status=200,
                payload={
                    "data": [
                        {
                            "mac": "sw:mac",
                            "_id": "device-id-123",
                            "port_overrides": [],
                            "port_table": [{"port_idx": 2, "portconf_id": "conf-2"}],
                        }
                    ]
                },
            )
            mocked.put(
                "https://udm.local:443/proxy/network/api/s/default/rest/device/device-id-123",
                status=200,
                payload={"data": []},
            )
            await client.set_port_poe_mode("sw:mac", 2, "auto")

        calls = _calls_for(mocked, "PUT", "/rest/device/device-id-123")
        body = calls[0].kwargs["json"]
        assert body["port_overrides"] == [{"port_idx": 2, "poe_mode": "auto", "portconf_id": "conf-2"}]


@pytest.mark.asyncio
async def test_get_site_role_returns_matching_site_role(api_module) -> None:
    """Request shape cross-checked against aiounifi's SiteListRequest -
    a global (non-site-scoped) GET, unlike every other call in this file.
    """
    async with aiohttp.ClientSession() as session:
        client = api_module.UniFiClient(session, "udm.local", "user", "pass", site="default")
        with aioresponses() as mocked:
            mocked.get("https://udm.local:443/", status=200)
            mocked.post(
                "https://udm.local:443/api/auth/login",
                status=200,
                payload={},
                headers={"X-CSRF-Token": "csrf-123"},
            )
            mocked.get(
                "https://udm.local:443/proxy/network/api/self/sites",
                status=200,
                payload={"data": [{"name": "default", "role": "readonly"}, {"name": "other", "role": "admin"}]},
            )
            role = await client.get_site_role()

        assert role == "readonly"
        assert client.site_role == "readonly"


@pytest.mark.asyncio
async def test_get_site_role_no_matching_site_returns_none(api_module) -> None:
    async with aiohttp.ClientSession() as session:
        client = api_module.UniFiClient(session, "udm.local", "user", "pass", site="default")
        with aioresponses() as mocked:
            mocked.get("https://udm.local:443/", status=200)
            mocked.post(
                "https://udm.local:443/api/auth/login",
                status=200,
                payload={},
                headers={"X-CSRF-Token": "csrf-123"},
            )
            mocked.get(
                "https://udm.local:443/proxy/network/api/self/sites",
                status=200,
                payload={"data": [{"name": "other-site", "role": "admin"}]},
            )
            role = await client.get_site_role()

        assert role is None


@pytest.mark.asyncio
async def test_get_site_role_request_failure_returns_none_not_raises(api_module) -> None:
    """Best-effort: a failure here must not break normal integration setup."""
    async with aiohttp.ClientSession() as session:
        client = api_module.UniFiClient(session, "udm.local", "user", "pass", site="default")
        with aioresponses() as mocked:
            mocked.get("https://udm.local:443/", status=200)
            mocked.post(
                "https://udm.local:443/api/auth/login",
                status=200,
                payload={},
                headers={"X-CSRF-Token": "csrf-123"},
            )
            mocked.get(
                "https://udm.local:443/proxy/network/api/self/sites",
                status=403,
                payload={"error": "forbidden"},
            )
            role = await client.get_site_role()

        assert role is None
