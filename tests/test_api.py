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
